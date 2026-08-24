"""RQ workers for customer generation and persisted WhatsApp delivery."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta

from redis import Redis
from rq import Queue
from rq.job import JobStatus
from sqlalchemy import func, select, text

from app.agent.owner_chat_provider import (
    OwnerChatProvider,
    OwnerChatProviderError,
    OwnerChatRequest,
    ProviderBusinessProfile,
    ProviderKnowledge,
    ProviderMessage,
    ProviderSource,
    ProviderWorkingDay,
    ProviderWorkingShift,
    get_owner_chat_provider,
)
from app.channels.contracts import ChannelError, MessagingChannelAdapter
from app.channels.meta import MetaWhatsAppAdapter
from app.channels.privacy import CustomerIdentityUnavailable, decrypt_identity
from app.channels.profiles import ChannelProfileRegistry, ChannelProfileUnavailable
from app.core.config import Settings, get_settings
from app.core.exceptions import ApplicationError
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


def _queue(settings: Settings) -> Queue:
    return Queue(
        settings.customer_message_queue_name,
        connection=Redis.from_url(settings.redis_url),
    )


def _enqueue_once(
    job_id: str, function: object, identifier: uuid.UUID, settings: Settings
) -> None:
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
        existing.delete()
    queue.enqueue(
        function,
        str(identifier),
        job_id=job_id,
        job_timeout=settings.customer_generation_lease_seconds,
    )


def enqueue_inbound_message(message_id: uuid.UUID, settings: Settings) -> None:
    _enqueue_once(
        f"customer-inbound-{message_id}", process_inbound_message, message_id, settings
    )


def enqueue_outbound_message(
    message_id: uuid.UUID, settings: Settings | None = None, *, delay_seconds: int = 0
) -> None:
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
        existing.delete()
    if delay_seconds > 0:
        queue.enqueue_in(
            timedelta(seconds=delay_seconds),
            process_outbound_message,
            str(message_id),
            job_id=job_id,
            job_timeout=resolved.whatsapp_request_timeout_seconds + 5,
        )
    else:
        queue.enqueue(
            process_outbound_message,
            str(message_id),
            job_id=job_id,
            job_timeout=resolved.whatsapp_request_timeout_seconds + 5,
        )


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
    if arabic:
        return "عذراً، ما فيني اتبع هالطلب أو اكشف تعليمات داخلية."
    return "Sorry, I can’t follow that request or reveal internal instructions."


def _persist_reply(session, inbound: CustomerMessage, content: str) -> CustomerMessage:
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
    return OwnerChatRequest(
        profile=_profile(business),
        knowledge=knowledge,
        messages=(ProviderMessage(role="owner", content=message.content),),
        requested_at=now,
        max_output_tokens=settings.customer_chat_max_output_tokens,
        sources=sources,
        mode="customer",
    )


def process_inbound_message(
    message_id: str,
    provider: OwnerChatProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    settings_override: Settings | None = None,
) -> None:
    settings = settings_override or get_settings()
    identifier = uuid.UUID(message_id)
    reservation_id: uuid.UUID | None = None
    resolved_provider = provider or get_owner_chat_provider(settings)
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
        if not conversation or not connection or not business:
            message.status = CustomerMessageStatus.FAILED
            message.failure_code = "customer.context_unavailable"
            session.commit()
            return
        message.status = CustomerMessageStatus.PROCESSING
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
        if not _admit_rate(session, message, settings):
            message.status = CustomerMessageStatus.FAILED
            message.failure_code = "customer.rate_limited"
            session.commit()
            return

        try:
            request = _provider_request(
                session,
                message,
                business,
                resolved_provider,
                embedding_provider,
                settings,
            )
            claim = reserve_customer_message_usage(
                session,
                message=message,
                estimated_input_tokens=resolved_provider.estimate_input_tokens(request),
                max_output_tokens=settings.customer_chat_max_output_tokens,
                lease_seconds=settings.customer_generation_lease_seconds,
            )
            reservation_id = claim.id
            result = resolved_provider.generate(request)
            if result.proposed_knowledge:
                raise ValueError("customer_provider_proposed_knowledge")
            reply_text = SOURCE_LABEL_PATTERN.sub("", result.reply).strip()
            if not reply_text:
                raise ValueError("customer_provider_empty_reply")
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
            reconcile_ai_usage(
                session,
                reservation_id,
                usage=result.usage,
                outcome="completed",
                provider_identifier=result.provider_identifier,
                model_identifier=result.model_identifier,
                commit=False,
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
            enqueue_outbound_message(reply.id, settings)
        except OwnerChatProviderError as exc:
            if reservation_id is not None:
                reconcile_ai_usage(
                    session,
                    reservation_id,
                    usage=exc.usage,
                    outcome=("uncertain" if exc.usage_uncertain else "release"),
                    provider_identifier=exc.provider_identifier,
                    model_identifier=exc.model_identifier,
                    commit=False,
                )
            message.status = CustomerMessageStatus.FAILED
            message.failure_code = "customer.provider_failure"
            session.commit()
        except ApplicationError, ValueError:
            if reservation_id is not None:
                reconcile_ai_usage(
                    session, reservation_id, usage=None, outcome="release", commit=False
                )
            message.status = CustomerMessageStatus.FAILED
            message.failure_code = "customer.generation_failed"
            session.commit()


def process_outbound_message(
    message_id: str,
    adapter: MessagingChannelAdapter | None = None,
    settings_override: Settings | None = None,
) -> None:
    settings = settings_override or get_settings()
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
            session.commit()
            try:
                result = resolved_adapter.send_text(recipient, message.content)
            except ChannelError as exc:
                message = session.get(CustomerMessage, identifier)
                if (
                    exc.retryable
                    and message is not None
                    and message.send_attempts < settings.whatsapp_outbound_max_attempts
                ):
                    retry_delay = exc.retry_after_seconds or (5 * message.send_attempts)
                    message.status = CustomerMessageStatus.PENDING_SEND
                    message.next_attempt_at = utc_now() + timedelta(seconds=retry_delay)
                    message.failure_code = exc.code
                elif message is not None:
                    message.status = CustomerMessageStatus.FAILED
                    message.failure_code = exc.code
                session.commit()
            else:
                message = session.get(CustomerMessage, identifier)
                if message is not None:
                    message.status = CustomerMessageStatus.SENT
                    message.provider_message_id = result.provider_message_id
                    message.next_attempt_at = None
                    message.failure_code = None
                    session.commit()
    if retry_delay is not None:
        enqueue_outbound_message(identifier, settings, delay_seconds=retry_delay)
