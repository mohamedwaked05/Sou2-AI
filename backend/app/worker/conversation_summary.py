"""Asynchronous rolling summaries for completed owner conversation turns."""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import timedelta

from redis import Redis
from rq import Queue, Retry
from rq.job import JobStatus
from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased

from app.agent.owner_chat_provider import (
    ConversationSummaryRequest,
    OwnerChatProviderError,
    ProviderMessage,
    _validated_summary,
    create_owner_chat_provider,
    deterministic_conversation_summary,
    summary_safe_content,
)
from app.core.admission import require_open_admission
from app.core.config import Settings, get_settings
from app.core.runtime import (
    job_contract,
    preserve_incompatible_failed_job,
    validate_worker_job,
    worker_provider_settings,
)
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
    summary_attempt_has_usage,
)

RECENT_TURNS_TO_KEEP = 6
SUMMARY_BATCH_TURNS = 10
SUMMARY_MAX_OUTPUT_TOKENS = 256
SUMMARY_MAX_CHARACTERS = 2000
logger = logging.getLogger(__name__)


def summary_job_id(conversation_id: uuid.UUID) -> str:
    return f"owner-conversation-summary-{conversation_id}"


def enqueue_conversation_summary(
    conversation_id: uuid.UUID, settings: Settings
) -> None:
    require_open_admission(settings)
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
        preserve_incompatible_failed_job(existing, settings, "summary")
        existing.delete()
    queue.enqueue(
        process_conversation_summary,
        str(conversation_id),
        job_id=job_id,
        job_timeout=settings.owner_chat_generation_lease_seconds,
        retry=Retry(max=2, interval=[2, 8]),
        meta={"runtime_contract": job_contract(settings, "summary")},
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
        now = session.scalar(select(func.clock_timestamp()))
        if (
            summary.generation_state == ConversationSummaryState.PROCESSING
            and summary.generation_claim_expires_at is not None
            and summary.generation_claim_expires_at > now
        ):
            session.rollback()
            return None
        failure = None
        if (
            summary.content
            and summary.provider_identifier == "mock"
            and summary.model_identifier != "deterministic-v2"
        ):
            failure = "summary_legacy_review_required"
        elif (
            summary.last_charged_through_sequence_number
            > summary.summarized_through_sequence_number
        ):
            failure = "summary_checkpoint_already_charged"
        elif summary.last_failure_code == "summary_expired_dispatch_review_required":
            failure = "summary_expired_dispatch_review_required"
        elif summary.generation_state in {
            ConversationSummaryState.PROCESSING,
            ConversationSummaryState.FAILED,
        } and summary_attempt_has_usage(session, summary):
            failure = "summary_expired_dispatch_review_required"
        if failure:
            summary.generation_state = ConversationSummaryState.FAILED
            summary.pending_through_sequence_number = None
            summary.generation_claim_token = None
            summary.generation_claim_expires_at = None
            summary.last_failure_code = failure
            session.commit()
            logger.warning(
                "conversation_summary_blocked reason=%s summary_id=%s",
                failure,
                summary.id,
            )
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
        if settings.conversation_summary_provider == "mock":
            while batch:
                try:
                    deterministic_conversation_summary(
                        ConversationSummaryRequest(
                            previous_summary=summary.content,
                            messages=tuple(
                                ProviderMessage(
                                    role=str(message.role),
                                    content=summary_safe_content(message.content),
                                )
                                for turn in batch
                                for message in turn
                            ),
                        )
                    )
                    break
                except ValueError:
                    batch = batch[:-1]
            if not batch:
                summary.generation_state = ConversationSummaryState.FAILED
                summary.pending_through_sequence_number = None
                summary.generation_claim_token = None
                summary.generation_claim_expires_at = None
                summary.last_failure_code = "summary_retention_limit"
                session.commit()
                logger.warning(
                    "conversation_summary_blocked reason=summary_retention_limit "
                    "summary_id=%s",
                    summary.id,
                )
                return None
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
            .execution_options(populate_existing=True)
        )
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
            if summary is not None and outcome != "release":
                summary.last_charged_through_sequence_number = max(
                    summary.last_charged_through_sequence_number,
                    summary.pending_through_sequence_number or 0,
                )
        if summary is None:
            session.commit()
            logger.warning(
                "conversation_summary_late_failure summary_id=%s reason=claim_replaced",
                summary_id,
            )
            return
        summary.generation_state = ConversationSummaryState.FAILED
        summary.pending_through_sequence_number = None
        summary.generation_claim_token = None
        summary.generation_claim_expires_at = None
        summary.last_failure_code = code
        session.commit()


def process_conversation_summary(conversation_id: str) -> None:
    settings = get_settings()
    validate_worker_job(settings, "summary")
    identifier = uuid.UUID(conversation_id)
    claimed = _claim_summary(identifier, settings)
    if claimed is None:
        return
    summary_id, claim_token, checkpoint, target = claimed
    reservation: AIUsageReservationClaim | None = None
    try:
        with get_session_factory()() as session:
            summary = session.get(OwnerConversationSummary, summary_id)
            if (
                summary is None
                or summary.generation_claim_token != claim_token
                or summary.generation_claim_expires_at <= utc_now()
            ):
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
            provider = create_owner_chat_provider(
                worker_provider_settings(settings, "summary")
            )
            reservation = reserve_conversation_summary_usage(
                session,
                summary=summary,
                claim_token=claim_token,
                estimated_input_tokens=provider.estimate_summary_input_tokens(request),
                max_output_tokens=request.max_output_tokens,
                lease_seconds=settings.owner_chat_generation_lease_seconds,
            )
            now = session.scalar(select(func.clock_timestamp()))
            remaining = (summary.generation_claim_expires_at - now).total_seconds()
            remaining = min(remaining, (reservation.reset_at - now).total_seconds())
            request = replace(request, provider_timeout_seconds=remaining)
            if remaining <= 0:
                raise OwnerChatProviderError(
                    reason="summary_claim_expired", usage_uncertain=False
                )
            session.rollback()
        result = provider.summarize(request)
        try:
            clean = _validated_summary(result.summary)
            if result.retained_message_count != len(messages) or (
                request.previous_summary
                and request.previous_summary.strip().startswith(clean)
            ):
                raise ValueError("summary_retention_failed")
            if result.usage is None:
                raise ValueError("summary_usage_missing")
            if result.usage.output_tokens > request.max_output_tokens:
                raise ValueError("summary_output_limit")
        except ValueError as exc:
            reason = (
                str(exc)
                if str(exc)
                in {
                    "summary_retention_failed",
                    "summary_usage_missing",
                    "summary_output_limit",
                }
                else "summary_content_invalid"
            )
            raise OwnerChatProviderError(
                reason=reason,
                usage=result.usage,
                provider_identifier=result.provider_identifier,
                model_identifier=result.model_identifier,
            ) from None
        with get_session_factory()() as session:
            summary = session.scalar(
                select(OwnerConversationSummary)
                .where(
                    OwnerConversationSummary.id == summary_id,
                    OwnerConversationSummary.generation_claim_token == claim_token,
                    OwnerConversationSummary.pending_through_sequence_number == target,
                    OwnerConversationSummary.generation_state
                    == ConversationSummaryState.PROCESSING,
                    OwnerConversationSummary.generation_claim_expires_at
                    > func.clock_timestamp(),
                    OwnerConversationSummary.conversation_id == identifier,
                )
                .with_for_update()
            )
            if summary is None:
                raise OwnerChatProviderError(
                    reason="summary_claim_expired", usage=result.usage
                )
            if summary.generation_claim_expires_at <= session.scalar(
                select(func.clock_timestamp())
            ):
                raise OwnerChatProviderError(
                    reason="summary_claim_expired", usage=result.usage
                )
            reconcile_ai_usage(
                session,
                reservation.id,
                usage=result.usage,
                outcome="completed",
                provider_identifier=result.provider_identifier,
                model_identifier=result.model_identifier,
                commit=False,
                require_new=True,
                finalization_deadline=summary.generation_claim_expires_at,
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
        logger.warning(
            "conversation_summary_failed summary_id=%s reason=%s",
            summary_id,
            exc.reason,
        )
        _fail_summary(
            summary_id, claim_token, "summary_provider_failure", reservation, exc
        )
    except Exception as exc:
        logger.error(
            "conversation_summary_failed summary_id=%s exception_type=%s",
            summary_id,
            type(exc).__name__,
        )
        _fail_summary(summary_id, claim_token, "summary_unavailable", reservation)
