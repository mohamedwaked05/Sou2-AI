"""RQ workers for customer generation and persisted WhatsApp delivery."""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import replace
from datetime import datetime, timedelta

from redis import Redis
from rq import Queue
from rq.job import JobStatus
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.agent.owner_chat_provider import (
    OwnerChatProvider,
    OwnerChatProviderError,
    OwnerChatRequest,
    OwnerChatResult,
    ProviderBusinessProfile,
    ProviderKnowledge,
    ProviderMessage,
    ProviderSource,
    ProviderWorkingDay,
    ProviderWorkingShift,
    TokenUsage,
    get_owner_chat_provider,
)
from app.channels.contracts import ChannelError, MessagingChannelAdapter
from app.channels.meta import MetaWhatsAppAdapter
from app.channels.privacy import CustomerIdentityUnavailable, decrypt_identity
from app.channels.profiles import ChannelProfileRegistry, ChannelProfileUnavailable
from app.core.admission import require_open_admission
from app.core.config import Settings, get_settings
from app.core.exceptions import ApplicationError
from app.core.runtime import (
    job_contract,
    preserve_incompatible_failed_job,
    validate_worker_job,
    worker_provider_settings,
)
from app.core.security import utc_now
from app.database.models import (
    Business,
    BusinessKnowledge,
    BusinessStatus,
    CustomerConversation,
    CustomerConversationState,
    CustomerGenerationRateEvent,
    CustomerMessage,
    CustomerMessageStatus,
    InboundWebhookDelivery,
    KnowledgeDocument,
    KnowledgeDocumentStatus,
    MessagingChannelConnection,
    MessagingConnectionStatus,
)
from app.database.session import get_session_factory
from app.rag.embeddings import EmbeddingProvider, create_embedding_provider
from app.rag.retrieval import retrieve
from app.services.ai_usage import (
    reconcile_ai_usage,
    reserve_customer_message_usage,
)

HANDOFF_PATTERN = re.compile(
    r"(?i)(?:\bhuman\b|\breal person\b|\bagent\b|\bmanager\b|"
    r"موظف|انسان|إنسان|حدا حقيقي|احكي مع حدا|"
    r"\b(?:bade|baddi|badde|bdi) (?:e7ke|ehke) ma3 (?:hada|7ada|insan)\b)"
)
PRIVATE_OPERATION_PATTERN = re.compile(
    r"(?i)(?:inventory|stock quantity|sales|revenue|best sell|restock|"
    r"المخزون|المبيعات|الإيرادات|قديش عنا|كم عنا|"
    r"\b(?:stock|inventory|sales|revenue|adde 3anna|kam 3anna)\b)"
)
INJECTION_PATTERN = re.compile(
    r"(?i)(?:ignore (?:all |the )?(?:previous|system)|system prompt|developer message|"
    r"reveal (?:your |the )?(?:prompt|secret|token)|تعليمات النظام|تجاهل التعليمات)"
)
SOURCE_LABEL_PATTERN = re.compile(r"\s*\[S[1-9][0-9]*\]")
BUSINESS_QUESTION_PATTERN = re.compile(
    r"(?i)\b(?:deliver(?:y|ies)?|shipping|price|cost|how much|tomorrow|when do you|"
    r"open|hours|address|location|return|refund|warranty|menu|products?)\b|"
    r"توصيل|السعر|قديش|بكرا|العنوان"
)
logger = logging.getLogger(__name__)


class CustomerClaimLost(RuntimeError):
    """A stale result must not change a persisted customer claim."""


def _locked_customer_claim(
    session: Session,
    identifier: uuid.UUID,
    business_id: uuid.UUID,
    conversation_id: uuid.UUID,
    status: CustomerMessageStatus,
    deadline: datetime,
    *,
    require_unexpired=True,
) -> CustomerMessage:
    message = session.scalar(
        select(CustomerMessage)
        .where(
            CustomerMessage.id == identifier,
            CustomerMessage.business_id == business_id,
            CustomerMessage.conversation_id == conversation_id,
            CustomerMessage.status == status,
            CustomerMessage.claim_expires_at == deadline,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if message is None or (
        require_unexpired and deadline <= session.scalar(select(func.clock_timestamp()))
    ):
        raise CustomerClaimLost("customer_claim_lost")
    return message


def _fail_customer_generation(
    session: Session,
    identifier: uuid.UUID,
    business_id: uuid.UUID,
    conversation_id: uuid.UUID,
    deadline: datetime,
    reservation_id: uuid.UUID | None,
    usage: TokenUsage | None,
    outcome: str,
    provider_identifier: str | None,
    model_identifier: str | None,
    failure_code: str,
) -> None:
    # Match recovery/success lock order: message first, reservation second.
    try:
        message = _locked_customer_claim(
            session,
            identifier,
            business_id,
            conversation_id,
            CustomerMessageStatus.PROCESSING,
            deadline,
            require_unexpired=False,
        )
    except CustomerClaimLost:
        message = None
        logger.warning(
            "customer_late_result message_id=%s reason=claim_replaced", identifier
        )
    if reservation_id is not None:
        reconcile_ai_usage(
            session,
            reservation_id,
            usage=usage,
            outcome=outcome,
            provider_identifier=provider_identifier,
            model_identifier=model_identifier,
            commit=False,
        )
    if message is not None:
        message.status = CustomerMessageStatus.FAILED
        message.failure_code = failure_code
        message.claim_expires_at = None
    session.commit()


def _queue(settings: Settings) -> Queue:
    return Queue(
        settings.customer_message_queue_name,
        connection=Redis.from_url(settings.redis_url),
    )


def _enqueue_once(
    job_id: str, function: object, identifier: uuid.UUID, settings: Settings
) -> None:
    require_open_admission(settings)
    queue = _queue(settings)
    existing = queue.fetch_job(job_id)
    if existing is not None and existing.get_status(refresh=True) in {
        JobStatus.QUEUED,
        JobStatus.STARTED,
        JobStatus.DEFERRED,
        JobStatus.SCHEDULED,
    }:
        return
    if existing is not None:
        preserve_incompatible_failed_job(existing, settings, "customer")
        existing.delete()
    queue.enqueue(
        function,
        str(identifier),
        job_id=job_id,
        job_timeout=settings.customer_generation_lease_seconds,
        meta={"runtime_contract": job_contract(settings, "customer")},
    )


def enqueue_inbound_message(message_id: uuid.UUID, settings: Settings) -> None:
    _enqueue_once(
        f"customer-inbound-{message_id}", process_inbound_message, message_id, settings
    )


def enqueue_outbound_message(
    message_id: uuid.UUID, settings: Settings | None = None, *, delay_seconds: int = 0
) -> None:
    require_open_admission(settings)
    resolved = settings or get_settings()
    queue = _queue(resolved)
    job_id = (
        f"customer-outbound-{message_id}"
        if delay_seconds <= 0
        else (
            f"customer-outbound-{message_id}-retry-"
            f"{int(utc_now().timestamp()) + delay_seconds}"
        )
    )
    existing = queue.fetch_job(job_id)
    if existing is not None and existing.get_status(refresh=True) in {
        JobStatus.QUEUED,
        JobStatus.STARTED,
        JobStatus.DEFERRED,
        JobStatus.SCHEDULED,
    }:
        return
    if existing is not None:
        preserve_incompatible_failed_job(existing, resolved, "outbound")
        existing.delete()
    if delay_seconds > 0:
        queue.enqueue_in(
            timedelta(seconds=delay_seconds),
            process_outbound_message,
            str(message_id),
            job_id=job_id,
            job_timeout=resolved.whatsapp_request_timeout_seconds + 5,
            meta={"runtime_contract": job_contract(resolved, "outbound")},
        )
    else:
        queue.enqueue(
            process_outbound_message,
            str(message_id),
            job_id=job_id,
            job_timeout=resolved.whatsapp_request_timeout_seconds + 5,
            meta={"runtime_contract": job_contract(resolved, "outbound")},
        )


def recover_expired_customer_message_claims(
    settings_override: Settings | None = None, *, batch_size: int = 50
) -> int:
    """Bounded, idempotent recovery for jobs lost after their state commit."""
    require_open_admission(settings_override)
    now = utc_now()
    recovered = 0
    with get_session_factory()() as session:
        rows = session.scalars(
            select(CustomerMessage)
            .where(
                CustomerMessage.status.in_(
                    (CustomerMessageStatus.PROCESSING, CustomerMessageStatus.SENDING)
                ),
                CustomerMessage.claim_expires_at.is_not(None),
                CustomerMessage.claim_expires_at <= now,
            )
            .order_by(CustomerMessage.claim_expires_at, CustomerMessage.id)
            .limit(min(max(batch_size, 1), 100))
            .with_for_update(skip_locked=True)
        ).all()
        for message in rows:
            if message.status == CustomerMessageStatus.PROCESSING:
                session.execute(
                    text("SELECT public.sou2ai_recover_customer_usage(:message_id)"),
                    {"message_id": message.id},
                )
                message.status = CustomerMessageStatus.FAILED
                message.failure_code = "customer.processing_lease_expired"
            else:
                message.status = CustomerMessageStatus.FAILED
                message.failure_code = "channel.delivery_uncertain"
            message.claim_expires_at = None
            recovered += 1
        if rows:
            session.commit()
    return recovered


def _profile(business: Business) -> ProviderBusinessProfile:
    weekdays = (
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    )
    by_day = {day.day_of_week: day for day in business.opening_days}
    working_days: list[ProviderWorkingDay] = []
    for index, weekday in enumerate(weekdays):
        day = by_day.get(index)
        working_days.append(
            ProviderWorkingDay(
                weekday=weekday,  # type: ignore[arg-type]
                is_open=bool(day and day.is_open),
                shifts=tuple(
                    ProviderWorkingShift(start=shift.opens_at, end=shift.closes_at)
                    for shift in sorted(
                        day.shifts if day else [], key=lambda item: item.opens_at
                    )
                ),
            )
        )
    return ProviderBusinessProfile(
        name=business.name,
        description=business.description or "",
        category=str(business.category or ""),
        governorate=business.governorate or "",
        district=business.district or "",
        city=business.city or "",
        address_line=business.address_line or "",
        timezone=business.timezone,
        working_hours=tuple(working_days),
    )


def _static_reply(content: str, kind: str) -> str:
    arabic = bool(re.search(r"[\u0600-\u06ff]", content))
    franco = bool(re.search(r"(?i)\b(?:bade|baddi|badde|e7ke|adde|kam)\b", content))
    if kind == "handoff":
        if arabic:
            return "أكيد، تم تحويل المحادثة لفريق العمل. سيردّ عليك شخص قريباً."
        if franco:
            return "أكيد، 7awwalna el mo7adase la 7ada men el team w byerodd 3lek."
        return (
            "Of course — I’ve handed this conversation to the team. "
            "A person will reply soon."
        )
    if kind == "private":
        if arabic:
            return "عذراً، ما فيني شارك معلومات تشغيلية أو خاصة عبر محادثة العملاء."
        return (
            "Sorry, private or live operational information is unavailable "
            "in customer chat."
        )
    if kind == "missing":
        if arabic:
            return "عذراً، هيدا المعلومة مش متوفرة حالياً."
        if franco:
            return "Sorry, hal ma3loume mish mawjoude 3anna halla2."
        return "Sorry, that business information is not available right now."
    if arabic:
        return "عذراً، ما فيني اتبع هالطلب أو اكشف تعليمات داخلية."
    return "Sorry, I can’t follow that request or reveal internal instructions."


def _persist_reply(session, inbound: CustomerMessage, content: str) -> CustomerMessage:
    inbound = _locked_customer_claim(
        session,
        inbound.id,
        inbound.business_id,
        inbound.conversation_id,
        CustomerMessageStatus.PROCESSING,
        inbound.claim_expires_at,
    )
    reply = CustomerMessage(
        id=uuid.uuid4(),
        business_id=inbound.business_id,
        conversation_id=inbound.conversation_id,
        direction="outbound",
        sender="ai",
        content=content[:4000],
        status=CustomerMessageStatus.PENDING_SEND,
        reply_to_message_id=inbound.id,
    )
    session.add(reply)
    inbound.status = CustomerMessageStatus.COMPLETED
    inbound.claim_expires_at = None
    inbound.failure_code = None
    delivery = session.scalar(
        select(InboundWebhookDelivery).where(
            InboundWebhookDelivery.customer_message_id == inbound.id,
            InboundWebhookDelivery.event_kind == "message",
        )
    )
    if delivery is not None:
        delivery.status = "PROCESSED"
        delivery.processed_at = utc_now()
    session.commit()
    return reply


def _admit_rate(session, message: CustomerMessage, settings: Settings) -> bool:
    session.execute(
        text("SELECT pg_catalog.pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"customer-rate:{message.business_id}"},
    )
    if session.scalar(
        select(CustomerGenerationRateEvent.id).where(
            CustomerGenerationRateEvent.customer_message_id == message.id
        )
    ):
        return True
    since = utc_now() - timedelta(hours=1)
    conversation_count = (
        session.scalar(
            select(func.count())
            .select_from(CustomerGenerationRateEvent)
            .where(
                CustomerGenerationRateEvent.conversation_id == message.conversation_id,
                CustomerGenerationRateEvent.created_at >= since,
            )
        )
        or 0
    )
    business_count = (
        session.scalar(
            select(func.count())
            .select_from(CustomerGenerationRateEvent)
            .where(
                CustomerGenerationRateEvent.business_id == message.business_id,
                CustomerGenerationRateEvent.created_at >= since,
            )
        )
        or 0
    )
    if (
        conversation_count >= settings.customer_conversation_hourly_limit
        or business_count >= settings.customer_business_hourly_limit
    ):
        return False
    session.add(
        CustomerGenerationRateEvent(
            id=uuid.uuid4(),
            business_id=message.business_id,
            conversation_id=message.conversation_id,
            customer_message_id=message.id,
        )
    )
    session.commit()
    return True


def _provider_request(
    session,
    message: CustomerMessage,
    business: Business,
    provider: OwnerChatProvider,
    embedding_provider: EmbeddingProvider | None,
    settings: Settings,
) -> OwnerChatRequest:
    now = utc_now()
    knowledge_rows = session.scalars(
        select(BusinessKnowledge)
        .where(
            BusinessKnowledge.business_id == business.id,
            BusinessKnowledge.customer_visible.is_(True),
            (
                BusinessKnowledge.expires_at.is_(None)
                | (BusinessKnowledge.expires_at > now)
            ),
        )
        .order_by(BusinessKnowledge.updated_at.desc(), BusinessKnowledge.id)
        .limit(100)
    ).all()
    knowledge = tuple(
        ProviderKnowledge(
            subject_key=row.subject_key,
            content=row.content,
            category=str(row.category),
            expires_at=row.expires_at,
        )
        for row in knowledge_rows
    )
    sources: tuple[ProviderSource, ...] = ()
    has_documents = session.scalar(
        select(KnowledgeDocument.id)
        .where(
            KnowledgeDocument.business_id == business.id,
            KnowledgeDocument.customer_visible.is_(True),
            KnowledgeDocument.status == KnowledgeDocumentStatus.READY,
        )
        .limit(1)
    )
    if has_documents is not None:
        embedding = embedding_provider or create_embedding_provider(settings)
        result = retrieve(
            session,
            None,
            business.id,
            message.content,
            embedding,
            settings,
            request_id=str(message.id),
            customer_visible_only=True,
        )
        sources = tuple(
            ProviderSource(
                label=f"S{index}",
                document_id=str(chunk.document_id),
                filename=chunk.document_filename,
                chunk_id=str(chunk.chunk_id),
                content=chunk.content,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                section_title=chunk.section_title,
            )
            for index, chunk in enumerate(
                result.chunks[: settings.rag_context_max_chunks], 1
            )
        )
    history_rows = session.scalars(
        select(CustomerMessage)
        .where(
            CustomerMessage.business_id == business.id,
            CustomerMessage.conversation_id == message.conversation_id,
            (
                (CustomerMessage.id == message.id)
                | (
                    (CustomerMessage.direction == "inbound")
                    & (CustomerMessage.status == CustomerMessageStatus.COMPLETED)
                )
                | (
                    (CustomerMessage.direction == "outbound")
                    & CustomerMessage.sender.in_(("ai", "owner"))
                    & CustomerMessage.status.in_(
                        (
                            CustomerMessageStatus.SENT,
                            CustomerMessageStatus.DELIVERED,
                            CustomerMessageStatus.READ,
                        )
                    )
                )
            ),
        )
        .order_by(CustomerMessage.created_at.desc(), CustomerMessage.id.desc())
        .limit(12)
    ).all()
    history_rows.sort(key=lambda row: (row.created_at, row.id))
    return OwnerChatRequest(
        profile=_profile(business),
        knowledge=knowledge,
        messages=tuple(
            ProviderMessage(
                role="owner" if row.direction == "inbound" else "assistant",
                content=row.content,
            )
            for row in history_rows
        ),
        requested_at=now,
        max_output_tokens=settings.customer_chat_max_output_tokens,
        sources=sources,
        mode="customer",
    )


def _customer_evidence_supports(request: OwnerChatRequest, content: str) -> bool:
    """Use a conservative lexical gate before charging unsupported business turns."""
    if not BUSINESS_QUESTION_PATTERN.search(content):
        return True
    words = {
        word.casefold()
        for word in re.findall(r"[\w\u0600-\u06ff]+", content)
        if len(word) > 2
    }
    evidence = " ".join(
        [request.profile.name, request.profile.description]
        + [item.content for item in request.knowledge]
        + [item.content for item in request.sources]
    ).casefold()
    return any(word in evidence for word in words)


def _validate_customer_result(result, request: OwnerChatRequest, content: str) -> str:
    if result.proposed_knowledge:
        raise ValueError("customer_provider_proposed_knowledge")
    labels = tuple(result.cited_source_ids)
    valid = {source.label for source in request.sources}
    if len(labels) != len(set(labels)) or any(label not in valid for label in labels):
        raise ValueError("customer_provider_invalid_citations")
    if request.sources and BUSINESS_QUESTION_PATTERN.search(content) and not labels:
        raise ValueError("customer_provider_missing_citation")
    reply_text = SOURCE_LABEL_PATTERN.sub("", result.reply).strip()
    if not reply_text:
        raise ValueError("customer_provider_empty_reply")
    return reply_text


def process_inbound_message(
    message_id: str,
    provider: OwnerChatProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    settings_override: Settings | None = None,
) -> None:
    settings = settings_override or get_settings()
    validate_worker_job(settings, "customer")
    recover_expired_customer_message_claims(settings, batch_size=25)
    identifier = uuid.UUID(message_id)
    reservation_id: uuid.UUID | None = None
    generation_started = False
    result: OwnerChatResult | None = None
    resolved_provider = provider or get_owner_chat_provider(
        worker_provider_settings(settings, "customer")
    )
    with get_session_factory()() as session:
        message = session.scalar(
            select(CustomerMessage)
            .where(CustomerMessage.id == identifier)
            .with_for_update(skip_locked=True)
        )
        if message is None or message.status != CustomerMessageStatus.RECEIVED:
            return
        conversation = session.get(CustomerConversation, message.conversation_id)
        connection = (
            session.get(MessagingChannelConnection, conversation.connection_id)
            if conversation
            else None
        )
        business = session.get(Business, message.business_id)
        if (
            not conversation
            or not connection
            or not business
            or conversation.business_id != message.business_id
            or connection.business_id != message.business_id
        ):
            message.status = CustomerMessageStatus.FAILED
            message.failure_code = "customer.context_unavailable"
            message.claim_expires_at = None
            session.commit()
            return
        message.status = CustomerMessageStatus.PROCESSING
        message.claim_expires_at = utc_now() + timedelta(
            seconds=settings.customer_generation_lease_seconds
        )
        claim_deadline = message.claim_expires_at
        scope_business_id, scope_conversation_id = (
            message.business_id,
            message.conversation_id,
        )
        session.commit()

        if HANDOFF_PATTERN.search(message.content):
            conversation.state = CustomerConversationState.HUMAN_HANDOFF
            reply = _persist_reply(
                session, message, _static_reply(message.content, "handoff")
            )
            enqueue_outbound_message(reply.id, settings)
            return
        if (
            business.status != BusinessStatus.ACTIVE
            or connection.status != MessagingConnectionStatus.ACTIVE
            or not connection.auto_reply_enabled
            or conversation.state == CustomerConversationState.HUMAN_HANDOFF
        ):
            message.status = CustomerMessageStatus.COMPLETED
            message.claim_expires_at = None
            session.commit()
            return
        if PRIVATE_OPERATION_PATTERN.search(message.content):
            reply = _persist_reply(
                session, message, _static_reply(message.content, "private")
            )
            enqueue_outbound_message(reply.id, settings)
            return
        if INJECTION_PATTERN.search(message.content):
            reply = _persist_reply(
                session, message, _static_reply(message.content, "injection")
            )
            enqueue_outbound_message(reply.id, settings)
            return
        request = _provider_request(
            session,
            message,
            business,
            resolved_provider,
            embedding_provider,
            settings,
        )
        if not _customer_evidence_supports(request, message.content):
            reply = _persist_reply(
                session, message, _static_reply(message.content, "missing")
            )
            enqueue_outbound_message(reply.id, settings)
            return
        if not _admit_rate(session, message, settings):
            message.status = CustomerMessageStatus.FAILED
            message.failure_code = "customer.rate_limited"
            message.claim_expires_at = None
            session.commit()
            return

        try:
            message = _locked_customer_claim(
                session,
                identifier,
                scope_business_id,
                scope_conversation_id,
                CustomerMessageStatus.PROCESSING,
                claim_deadline,
            )
            claim = reserve_customer_message_usage(
                session,
                message=message,
                estimated_input_tokens=resolved_provider.estimate_input_tokens(request),
                max_output_tokens=settings.customer_chat_max_output_tokens,
                lease_seconds=settings.customer_generation_lease_seconds,
            )
            reservation_id = claim.id
            now = session.scalar(select(func.clock_timestamp()))
            remaining = min(
                (claim_deadline - now).total_seconds(),
                (claim.reset_at - now).total_seconds(),
            )
            if remaining <= 0:
                raise CustomerClaimLost("customer_claim_expired_before_dispatch")
            request = replace(request, provider_timeout_seconds=remaining)
            session.rollback()
            generation_started = True
            result = resolved_provider.generate(request)
            reply_text = _validate_customer_result(result, request, message.content)
            message = _locked_customer_claim(
                session,
                identifier,
                scope_business_id,
                scope_conversation_id,
                CustomerMessageStatus.PROCESSING,
                claim_deadline,
            )
            reply = CustomerMessage(
                id=uuid.uuid4(),
                business_id=message.business_id,
                conversation_id=message.conversation_id,
                direction="outbound",
                sender="ai",
                content=reply_text[:4000],
                status=CustomerMessageStatus.PENDING_SEND,
                reply_to_message_id=message.id,
            )
            session.add(reply)
            message.status = CustomerMessageStatus.COMPLETED
            message.claim_expires_at = None
            reconcile_ai_usage(
                session,
                reservation_id,
                usage=result.usage,
                outcome="completed",
                provider_identifier=result.provider_identifier,
                model_identifier=result.model_identifier,
                commit=False,
                require_new=True,
                finalization_deadline=claim_deadline,
            )
            delivery = session.scalar(
                select(InboundWebhookDelivery).where(
                    InboundWebhookDelivery.customer_message_id == message.id,
                    InboundWebhookDelivery.event_kind == "message",
                )
            )
            if delivery:
                delivery.status = "PROCESSED"
                delivery.processed_at = utc_now()
            session.commit()
        except OwnerChatProviderError as exc:
            session.rollback()
            logger.warning(
                "customer_provider_failed message_id=%s reason=%s",
                identifier,
                exc.reason,
            )
            _fail_customer_generation(
                session,
                identifier,
                scope_business_id,
                scope_conversation_id,
                claim_deadline,
                reservation_id,
                exc.usage,
                "reported_failure"
                if exc.usage is not None
                else "uncertain"
                if exc.usage_uncertain
                else "release",
                exc.provider_identifier,
                exc.model_identifier,
                "customer.provider_failure",
            )
        except Exception as exc:
            session.rollback()
            safe_reasons = {
                "customer_provider_proposed_knowledge",
                "customer_provider_invalid_citations",
                "customer_provider_missing_citation",
                "customer_provider_empty_reply",
            }
            reason = (
                str(exc)
                if isinstance(exc, ValueError) and str(exc) in safe_reasons
                else (
                    exc.error_code
                    if isinstance(exc, ApplicationError)
                    else "customer_generation_failed"
                )
            )
            logger.warning(
                "customer_generation_failed message_id=%s exception_type=%s reason=%s",
                identifier,
                type(exc).__name__,
                reason,
            )
            _fail_customer_generation(
                session,
                identifier,
                scope_business_id,
                scope_conversation_id,
                claim_deadline,
                reservation_id,
                result.usage if result is not None else None,
                "reported_failure"
                if result is not None and result.usage is not None
                else "uncertain"
                if generation_started
                else "release",
                result.provider_identifier if result is not None else None,
                result.model_identifier if result is not None else None,
                "customer.generation_failed",
            )
        else:
            enqueue_outbound_message(reply.id, settings)


def process_outbound_message(
    message_id: str,
    adapter: MessagingChannelAdapter | None = None,
    settings_override: Settings | None = None,
) -> None:
    settings = settings_override or get_settings()
    validate_worker_job(settings, "outbound")
    recover_expired_customer_message_claims(settings, batch_size=25)
    identifier = uuid.UUID(message_id)
    retry_delay: int | None = None
    with get_session_factory()() as session:
        message = session.scalar(
            select(CustomerMessage)
            .where(CustomerMessage.id == identifier)
            .with_for_update(skip_locked=True)
        )
        if message is None or message.status != CustomerMessageStatus.PENDING_SEND:
            return
        if message.next_attempt_at and message.next_attempt_at > utc_now():
            retry_delay = max(
                1, int((message.next_attempt_at - utc_now()).total_seconds())
            )
        else:
            conversation = session.get(CustomerConversation, message.conversation_id)
            connection = (
                session.get(MessagingChannelConnection, conversation.connection_id)
                if conversation
                else None
            )
            if (
                not conversation
                or not connection
                or connection.status != MessagingConnectionStatus.ACTIVE
            ):
                message.status = CustomerMessageStatus.FAILED
                message.failure_code = "channel.not_active"
                message.claim_expires_at = None
                session.commit()
                return
            try:
                recipient = decrypt_identity(
                    conversation.encrypted_customer_identity, settings
                )
                resolved_adapter = adapter
                if resolved_adapter is None:
                    profile = ChannelProfileRegistry(settings).resolve(
                        connection.connection_profile_key
                    )
                    resolved_adapter = MetaWhatsAppAdapter(profile)
            except ChannelProfileUnavailable, CustomerIdentityUnavailable:
                message.status = CustomerMessageStatus.FAILED
                message.failure_code = "channel.configuration_unavailable"
                session.commit()
                return
            message.status = CustomerMessageStatus.SENDING
            message.send_attempts += 1
            message.claim_expires_at = utc_now() + timedelta(
                seconds=settings.whatsapp_request_timeout_seconds + 5
            )
            claim_deadline = message.claim_expires_at
            scope_business_id, scope_conversation_id = (
                message.business_id,
                message.conversation_id,
            )
            session.commit()
            try:
                result = resolved_adapter.send_text(recipient, message.content)
            except ChannelError as exc:
                try:
                    message = _locked_customer_claim(
                        session,
                        identifier,
                        scope_business_id,
                        scope_conversation_id,
                        CustomerMessageStatus.SENDING,
                        claim_deadline,
                    )
                except CustomerClaimLost:
                    session.rollback()
                    logger.warning(
                        "customer_outbound_late_result message_id=%s "
                        "reason=claim_expired",
                        identifier,
                    )
                    return
                if (
                    exc.retryable
                    and message is not None
                    and message.send_attempts < settings.whatsapp_outbound_max_attempts
                ):
                    retry_delay = exc.retry_after_seconds or (5 * message.send_attempts)
                    message.status = CustomerMessageStatus.PENDING_SEND
                    message.claim_expires_at = None
                    message.next_attempt_at = utc_now() + timedelta(seconds=retry_delay)
                    message.failure_code = exc.code
                elif message is not None:
                    message.status = CustomerMessageStatus.FAILED
                    message.failure_code = exc.code
                    message.claim_expires_at = None
                session.commit()
            else:
                try:
                    message = _locked_customer_claim(
                        session,
                        identifier,
                        scope_business_id,
                        scope_conversation_id,
                        CustomerMessageStatus.SENDING,
                        claim_deadline,
                    )
                except CustomerClaimLost:
                    session.rollback()
                    logger.warning(
                        "customer_outbound_late_result message_id=%s "
                        "reason=delivery_uncertain",
                        identifier,
                    )
                    return
                if message is not None:
                    message.status = CustomerMessageStatus.SENT
                    message.claim_expires_at = None
                    message.provider_message_id = result.provider_message_id
                    message.next_attempt_at = None
                    message.failure_code = None
                    session.commit()
    if retry_delay is not None:
        enqueue_outbound_message(identifier, settings, delay_seconds=retry_delay)
