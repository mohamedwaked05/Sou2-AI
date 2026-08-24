"""Asynchronous rolling summaries for completed owner conversation turns."""

from __future__ import annotations

import uuid
from datetime import timedelta

from redis import Redis
from rq import Queue, Retry
from rq.job import JobStatus
from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased

from app.agent.owner_chat_provider import (
    ConversationSummaryRequest,
    OwnerChatProviderError,
    ProviderMessage,
    create_owner_chat_provider,
    summary_safe_content,
)
from app.core.config import Settings, get_settings
from app.core.security import utc_now
from app.database.models import (
    ChatGenerationState,
    ChatMessageRole,
    ConversationSummaryState,
    OwnerChatMessage,
    OwnerConversation,
    OwnerConversationSummary,
)
from app.database.session import get_session_factory
from app.services.ai_usage import (
    AIUsageReservationClaim,
    reconcile_ai_usage,
    reserve_conversation_summary_usage,
)

RECENT_TURNS_TO_KEEP = 6
SUMMARY_BATCH_TURNS = 10
SUMMARY_MAX_OUTPUT_TOKENS = 256
SUMMARY_MAX_CHARACTERS = 2000


def summary_job_id(conversation_id: uuid.UUID) -> str:
    return f"owner-conversation-summary-{conversation_id}"


def enqueue_conversation_summary(
    conversation_id: uuid.UUID, settings: Settings
) -> None:
    queue = Queue(
        settings.knowledge_queue_name, connection=Redis.from_url(settings.redis_url)
    )
    job_id = summary_job_id(conversation_id)
    existing = queue.fetch_job(job_id)
    if existing is not None:
        if existing.get_status(refresh=True) in {
            JobStatus.QUEUED,
            JobStatus.STARTED,
            JobStatus.DEFERRED,
            JobStatus.SCHEDULED,
        }:
            return
        existing.delete()
    queue.enqueue(
        process_conversation_summary,
        str(conversation_id),
        job_id=job_id,
        job_timeout=settings.knowledge_worker_timeout_seconds,
        retry=Retry(max=2, interval=[2, 8]),
    )


def _eligible_turns(session, conversation_id: uuid.UUID, after: int):
    assistant = aliased(OwnerChatMessage)
    return session.execute(
        select(OwnerChatMessage, assistant)
        .join(
            assistant,
            and_(
                assistant.reply_to_message_id == OwnerChatMessage.id,
                assistant.conversation_id == OwnerChatMessage.conversation_id,
                assistant.role == ChatMessageRole.ASSISTANT,
            ),
        )
        .where(
            OwnerChatMessage.conversation_id == conversation_id,
            OwnerChatMessage.role == ChatMessageRole.OWNER,
            OwnerChatMessage.generation_state == ChatGenerationState.COMPLETED,
            OwnerChatMessage.sequence_number > after,
        )
        .order_by(OwnerChatMessage.sequence_number, OwnerChatMessage.id)
    ).all()


def _claim_summary(
    conversation_id: uuid.UUID, settings: Settings
) -> tuple[uuid.UUID, uuid.UUID, int, int] | None:
    with get_session_factory()() as session:
        conversation = session.scalar(
            select(OwnerConversation).where(OwnerConversation.id == conversation_id)
        )
        if conversation is None:
            return None
        session.execute(
            insert(OwnerConversationSummary)
            .values(
                id=uuid.uuid4(),
                business_id=conversation.business_id,
                conversation_id=conversation.id,
            )
            .on_conflict_do_nothing(
                index_elements=[OwnerConversationSummary.conversation_id]
            )
        )
        session.commit()
        summary = session.scalar(
            select(OwnerConversationSummary)
            .where(OwnerConversationSummary.conversation_id == conversation_id)
            .with_for_update()
        )
        if summary is None:  # pragma: no cover - insert/constraint invariant
            return None
        now = utc_now()
        if (
            summary.generation_state == ConversationSummaryState.PROCESSING
            and summary.generation_claim_expires_at is not None
            and summary.generation_claim_expires_at > now
        ):
            session.rollback()
            return None
        turns = _eligible_turns(
            session, conversation_id, summary.summarized_through_sequence_number
        )
        if len(turns) <= RECENT_TURNS_TO_KEEP:
            summary.generation_state = ConversationSummaryState.IDLE
            summary.pending_through_sequence_number = None
            summary.generation_claim_token = None
            summary.generation_claim_expires_at = None
            session.commit()
            return None
        batch = turns[:-RECENT_TURNS_TO_KEEP][:SUMMARY_BATCH_TURNS]
        target = batch[-1][1].sequence_number
        if target <= summary.last_charged_through_sequence_number:
            summary.generation_state = ConversationSummaryState.FAILED
            summary.pending_through_sequence_number = None
            summary.generation_claim_token = None
            summary.generation_claim_expires_at = None
            summary.last_failure_code = "summary_checkpoint_already_charged"
            session.commit()
            return None
        claim_token = uuid.uuid4()
        summary.generation_state = ConversationSummaryState.PROCESSING
        summary.pending_through_sequence_number = target
        summary.generation_claim_token = claim_token
        summary.generation_claim_expires_at = now + timedelta(
            seconds=settings.owner_chat_generation_lease_seconds
        )
        summary.generation_attempts += 1
        session.commit()
        return (
            summary.id,
            claim_token,
            summary.summarized_through_sequence_number,
            target,
        )


def _fail_summary(
    summary_id: uuid.UUID,
    claim_token: uuid.UUID,
    code: str,
    reservation: AIUsageReservationClaim | None,
    error: OwnerChatProviderError | None = None,
) -> None:
    with get_session_factory()() as session:
        summary = session.scalar(
            select(OwnerConversationSummary)
            .where(
                OwnerConversationSummary.id == summary_id,
                OwnerConversationSummary.generation_claim_token == claim_token,
            )
            .with_for_update()
        )
        if summary is None:
            return
        if reservation is not None:
            outcome = (
                "reported_failure"
                if error is not None and error.usage is not None
                else "uncertain"
                if error is None or error.usage_uncertain
                else "release"
            )
            reconcile_ai_usage(
                session,
                reservation.id,
                usage=error.usage if error is not None else None,
                outcome=outcome,
                provider_identifier=(
                    error.provider_identifier if error is not None else None
                ),
                model_identifier=(
                    error.model_identifier if error is not None else None
                ),
                commit=False,
            )
            if outcome != "release":
                summary.last_charged_through_sequence_number = max(
                    summary.last_charged_through_sequence_number,
                    summary.pending_through_sequence_number or 0,
                )
        summary.generation_state = ConversationSummaryState.FAILED
        summary.pending_through_sequence_number = None
        summary.generation_claim_token = None
        summary.generation_claim_expires_at = None
        summary.last_failure_code = code
        session.commit()


def process_conversation_summary(conversation_id: str) -> None:
    settings = get_settings()
    identifier = uuid.UUID(conversation_id)
    claimed = _claim_summary(identifier, settings)
    if claimed is None:
        return
    summary_id, claim_token, checkpoint, target = claimed
    reservation: AIUsageReservationClaim | None = None
    try:
        with get_session_factory()() as session:
            summary = session.get(OwnerConversationSummary, summary_id)
            if summary is None:
                return
            turns = _eligible_turns(session, identifier, checkpoint)
            selected = [turn for turn in turns if turn[1].sequence_number <= target]
            messages = tuple(
                ProviderMessage(
                    role=str(message.role),
                    content=summary_safe_content(message.content),
                )
                for turn in selected
                for message in turn
            )
            request = ConversationSummaryRequest(
                previous_summary=summary.content,
                messages=messages,
                max_output_tokens=SUMMARY_MAX_OUTPUT_TOKENS,
            )
            provider = create_owner_chat_provider(settings)
            reservation = reserve_conversation_summary_usage(
                session,
                summary=summary,
                claim_token=claim_token,
                estimated_input_tokens=provider.estimate_summary_input_tokens(request),
                max_output_tokens=request.max_output_tokens,
                lease_seconds=settings.owner_chat_generation_lease_seconds,
            )
        result = provider.summarize(request)
        if result.usage is None or not result.summary.strip():
            raise OwnerChatProviderError(reason="invalid_summary")
        clean = result.summary.strip()[:SUMMARY_MAX_CHARACTERS]
        with get_session_factory()() as session:
            summary = session.scalar(
                select(OwnerConversationSummary)
                .where(
                    OwnerConversationSummary.id == summary_id,
                    OwnerConversationSummary.generation_claim_token == claim_token,
                    OwnerConversationSummary.pending_through_sequence_number == target,
                )
                .with_for_update()
            )
            if summary is None:
                return
            reconcile_ai_usage(
                session,
                reservation.id,
                usage=result.usage,
                outcome="completed",
                provider_identifier=result.provider_identifier,
                model_identifier=result.model_identifier,
                commit=False,
            )
            summary.content = clean
            summary.summarized_through_sequence_number = target
            summary.last_charged_through_sequence_number = max(
                summary.last_charged_through_sequence_number, target
            )
            summary.summary_version += 1
            summary.generation_state = ConversationSummaryState.IDLE
            summary.pending_through_sequence_number = None
            summary.generation_claim_token = None
            summary.generation_claim_expires_at = None
            summary.provider_identifier = result.provider_identifier
            summary.model_identifier = result.model_identifier
            summary.last_failure_code = None
            session.commit()
    except OwnerChatProviderError as exc:
        _fail_summary(
            summary_id, claim_token, "summary_provider_failure", reservation, exc
        )
    except Exception:
        _fail_summary(summary_id, claim_token, "summary_unavailable", reservation)
