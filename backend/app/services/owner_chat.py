"""Persistent, ordered owner-chat orchestration."""

from __future__ import annotations

import base64
import json
import math
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import timedelta

from fastapi import status
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.agent.owner_chat_provider import (
    OwnerChatProvider,
    OwnerChatProviderError,
    OwnerChatProviderInvalidResponse,
    OwnerChatRequest,
    OwnerChatResult,
    ProviderBusinessProfile,
    ProviderKnowledge,
    ProviderMessage,
    ProviderSource,
    ProviderWorkingDay,
    ProviderWorkingShift,
    TokenUsage,
    estimate_utf8_tokens,
)
from app.core.config import Settings
from app.core.exceptions import ApplicationError
from app.core.security import utc_now
from app.database.models import (
    Business,
    BusinessKnowledge,
    BusinessOpeningDay,
    BusinessStatus,
    ChatGenerationState,
    ChatMessageRole,
    OwnerChatCitation,
    OwnerChatMessage,
    OwnerConversation,
    User,
)
from app.rag.embeddings import create_embedding_provider
from app.rag.retrieval import retrieve
from app.schemas.owner_chat import (
    ChatMessageResponse,
    ConversationHistoryResponse,
    OwnerMessageRequest,
    OwnerTurnResponse,
)
from app.services.ai_usage import (
    AIUsageReservationClaim,
    reconcile_ai_usage,
    reserve_owner_chat_usage,
)
from app.services.api_limits import (
    admit_owner_chat_generation,
    undo_owner_chat_generation_admission,
)
from app.services.business_knowledge import upsert_proposed_knowledge
from app.services.business_profiles import is_business_profile_complete
from app.services.businesses import load_full_access_business

CHAT_CONTEXT_MESSAGE_LIMIT = 12
HISTORY_PAGE_SIZE = 50
HIGH_CONFIDENCE_EVIDENCE_SIMILARITY = 0.65
PROVIDER_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


@dataclass(frozen=True)
class _Claim:
    message_id: uuid.UUID
    token: uuid.UUID


@dataclass(frozen=True)
class _PreparedTurn:
    request: OwnerChatRequest
    business: Business
    has_usable_evidence: bool


def _provider_unavailable() -> ApplicationError:
    return ApplicationError(
        "The assistant is temporarily unavailable. Please retry.",
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        error_code="assistant_unavailable",
    )


def _eligible_business(
    session: Session, user: User, business_id: uuid.UUID
) -> Business:
    business = load_full_access_business(session, user, business_id)
    if business.status is not BusinessStatus.ACTIVE or not is_business_profile_complete(
        business
    ):
        raise ApplicationError(
            "This business is not active.",
            status_code=status.HTTP_403_FORBIDDEN,
            error_code="business_not_active",
        )
    return business


def _get_or_create_conversation(
    session: Session, business_id: uuid.UUID
) -> OwnerConversation:
    session.execute(
        insert(OwnerConversation)
        .values(id=uuid.uuid4(), business_id=business_id, next_turn_number=1)
        .on_conflict_do_nothing(index_elements=[OwnerConversation.business_id])
    )
    session.commit()
    conversation = session.scalar(
        select(OwnerConversation).where(OwnerConversation.business_id == business_id)
    )
    if conversation is None:  # pragma: no cover - protected by the unique insert
        raise _provider_unavailable()
    return conversation


def _create_or_reuse_owner_message(
    session: Session,
    conversation_id: uuid.UUID,
    body: OwnerMessageRequest,
) -> tuple[OwnerChatMessage, bool]:
    conversation = session.scalar(
        select(OwnerConversation)
        .where(OwnerConversation.id == conversation_id)
        .with_for_update(key_share=True)
    )
    if conversation is None:  # pragma: no cover - business owns the conversation
        raise _provider_unavailable()
    existing = session.scalar(
        select(OwnerChatMessage).where(
            OwnerChatMessage.conversation_id == conversation_id,
            OwnerChatMessage.idempotency_key == body.idempotency_key,
        )
    )
    if existing is not None:
        if existing.content != body.content:
            session.rollback()
            raise ApplicationError(
                "This idempotency key was already used with different content.",
                status_code=status.HTTP_409_CONFLICT,
                error_code="idempotency_conflict",
            )
        session.commit()
        return existing, True

    turn_number = conversation.next_turn_number
    conversation.next_turn_number += 1
    message = OwnerChatMessage(
        conversation_id=conversation.id,
        sequence_number=turn_number * 2 - 1,
        role=ChatMessageRole.OWNER,
        content=body.content,
        idempotency_key=body.idempotency_key,
        generation_state=ChatGenerationState.PENDING,
    )
    session.add(message)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raced = session.scalar(
            select(OwnerChatMessage).where(
                OwnerChatMessage.conversation_id == conversation_id,
                OwnerChatMessage.idempotency_key == body.idempotency_key,
            )
        )
        if raced is None:
            raise _provider_unavailable() from None
        if raced.content != body.content:
            raise ApplicationError(
                "This idempotency key was already used with different content.",
                status_code=status.HTTP_409_CONFLICT,
                error_code="idempotency_conflict",
            ) from None
        return raced, True
    return message, False


def _message_response(message: OwnerChatMessage) -> ChatMessageResponse:
    return ChatMessageResponse(
        id=message.id,
        sequence_number=message.sequence_number,
        role=message.role,
        content=message.content,
        created_at=message.created_at,
        sources=[
            {
                "label": citation.label,
                "document_id": citation.document_id,
                "filename": citation.filename,
                "page_start": citation.page_start,
                "page_end": citation.page_end,
                "section_title": citation.section_title,
                "available": citation.document_id is not None,
            }
            for citation in sorted(
                message.citations, key=lambda item: item.citation_order
            )
        ],
    )


def _completed_turn(
    session: Session, owner_message: OwnerChatMessage, replayed: bool
) -> OwnerTurnResponse | None:
    assistant = session.scalar(
        select(OwnerChatMessage)
        .where(
            OwnerChatMessage.conversation_id == owner_message.conversation_id,
            OwnerChatMessage.reply_to_message_id == owner_message.id,
            OwnerChatMessage.role == ChatMessageRole.ASSISTANT,
        )
        .options(selectinload(OwnerChatMessage.citations))
    )
    if assistant is None:
        return None
    return OwnerTurnResponse(
        owner_message=_message_response(owner_message),
        assistant_message=_message_response(assistant),
        replayed=replayed,
    )


def _claim_oldest_turn(
    session: Session,
    conversation_id: uuid.UUID,
    business_id: uuid.UUID,
    settings: Settings,
) -> _Claim | None:
    session.scalar(
        select(OwnerConversation)
        .where(OwnerConversation.id == conversation_id)
        .with_for_update(key_share=True)
    )
    now = utc_now()
    oldest = session.scalar(
        select(OwnerChatMessage)
        .where(
            OwnerChatMessage.conversation_id == conversation_id,
            OwnerChatMessage.role == ChatMessageRole.OWNER,
            OwnerChatMessage.generation_state != ChatGenerationState.COMPLETED,
        )
        .order_by(OwnerChatMessage.sequence_number, OwnerChatMessage.id)
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if oldest is None:
        session.commit()
        return None
    if (
        oldest.generation_state == ChatGenerationState.PROCESSING
        and oldest.generation_claim_expires_at is not None
        and oldest.generation_claim_expires_at > now
    ):
        session.commit()
        return None
    token = uuid.uuid4()
    next_attempt = oldest.generation_attempts + 1
    admit_owner_chat_generation(
        session,
        business_id=business_id,
        owner_message_id=oldest.id,
        generation_attempt=next_attempt,
    )
    oldest.generation_state = ChatGenerationState.PROCESSING
    oldest.generation_claim_token = token
    oldest.generation_claim_expires_at = now + timedelta(
        seconds=settings.owner_chat_generation_lease_seconds
    )
    oldest.generation_attempts = next_attempt
    message_id = oldest.id
    session.commit()
    return _Claim(message_id=message_id, token=token)


def _cosine_similarity(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    if denominator == 0:
        return 0
    return sum(a * b for a, b in zip(left, right, strict=True)) / denominator


def _evidence_terms(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    terms: set[str] = set()
    for term in re.findall(r"[^\W_]+", normalized, flags=re.UNICODE):
        if len(term) < 3:
            continue
        if term.endswith("ies") and len(term) > 4:
            term = f"{term[:-3]}y"
        elif term.endswith("s") and len(term) > 4:
            term = term[:-1]
        terms.add(term)
    return terms


def _has_meaningful_overlap(question: str, evidence: str) -> bool:
    ignored = {
        "about",
        "business",
        "document",
        "from",
        "have",
        "information",
        "please",
        "that",
        "the",
        "this",
        "what",
        "when",
        "where",
        "which",
        "with",
    }
    return bool((_evidence_terms(question) - ignored) & _evidence_terms(evidence))


def _profile_evidence_texts(profile: ProviderBusinessProfile) -> tuple[str, ...]:
    identity = (
        f"Business name: {profile.name}. Description: {profile.description}. "
        f"Category: {profile.category}. Location: {profile.address_line}, "
        f"{profile.city}, {profile.district}, {profile.governorate}."
    )
    hours: list[str] = []
    arabic_weekdays = (
        "الاثنين",
        "الثلاثاء",
        "الأربعاء",
        "الخميس",
        "الجمعة",
        "السبت",
        "الأحد",
    )
    lebanese_weekdays = (
        "التنين",
        "التلاتا",
        "الأربعا",
        "الخميس",
        "الجمعة",
        "السبت",
        "الأحد",
    )
    franco_weekdays = (
        "el tenein",
        "el telata",
        "el arb3a",
        "el khamis",
        "el jem3a",
        "el sabet",
        "el a7ad",
    )
    for index, day in enumerate(profile.working_hours):
        schedule = (
            "closed"
            if not day.is_open
            else ", ".join(
                f"{shift.start.isoformat(timespec='minutes')} to "
                f"{shift.end.isoformat(timespec='minutes')}"
                for shift in day.shifts
            )
        )
        hours.extend(
            (
                f"Opening hours: {day.weekday} is {schedule}.",
                f"ساعات العمل: يوم {arabic_weekdays[index]} هو {schedule}.",
                f"دوام المحل: نهار {lebanese_weekdays[index]} هو {schedule}.",
                f"wa2et el 3amal: nhar {franco_weekdays[index]} howwe {schedule}.",
            )
        )
    return (identity, *hours)


def _select_relevant_knowledge(
    records: list[BusinessKnowledge],
    current_message: str,
    similarities: tuple[float, ...],
    settings: Settings,
) -> tuple[ProviderKnowledge, ...]:
    ranked = sorted(
        zip(records, similarities, strict=True),
        key=lambda item: (item[1], item[0].updated_at, str(item[0].id)),
        reverse=True,
    )
    ordered = [
        record
        for record, similarity in ranked
        if similarity >= settings.retrieval_minimum_similarity
        or _has_meaningful_overlap(
            current_message, f"{record.subject_key} {record.content}"
        )
    ]
    return tuple(
        ProviderKnowledge(
            subject_key=record.subject_key,
            content=record.content,
            category=str(record.category),
            expires_at=record.expires_at,
        )
        for record in ordered
    )


def _build_provider_request(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    owner_message_id: uuid.UUID,
    settings: Settings,
) -> _PreparedTurn:
    owner_message = session.get(OwnerChatMessage, owner_message_id)
    business = session.scalar(
        select(Business)
        .where(Business.id == business_id)
        .options(
            selectinload(Business.opening_days).selectinload(BusinessOpeningDay.shifts)
        )
        .execution_options(populate_existing=True)
    )
    if owner_message is None or business is None:
        raise _provider_unavailable()
    messages = session.scalars(
        select(OwnerChatMessage)
        .where(
            OwnerChatMessage.conversation_id == owner_message.conversation_id,
            OwnerChatMessage.sequence_number <= owner_message.sequence_number,
        )
        .order_by(OwnerChatMessage.sequence_number.desc(), OwnerChatMessage.id.desc())
        .limit(CHAT_CONTEXT_MESSAGE_LIMIT)
    ).all()
    messages.reverse()
    now = utc_now()
    knowledge = session.scalars(
        select(BusinessKnowledge)
        .where(
            BusinessKnowledge.business_id == business_id,
            or_(
                BusinessKnowledge.expires_at.is_(None),
                BusinessKnowledge.expires_at > now,
            ),
        )
        .order_by(BusinessKnowledge.updated_at.desc(), BusinessKnowledge.id.desc())
        .limit(settings.owner_chat_knowledge_context_limit)
    ).all()
    profile = ProviderBusinessProfile(
        name=business.name,
        description=business.description or "",
        category=str(business.category or ""),
        governorate=business.governorate or "",
        district=business.district or "",
        city=business.city or "",
        address_line=business.address_line or "",
        timezone=business.timezone,
        working_hours=tuple(
            ProviderWorkingDay(
                weekday=PROVIDER_WEEKDAYS[day.day_of_week],
                is_open=day.is_open,
                shifts=tuple(
                    ProviderWorkingShift(start=shift.opens_at, end=shift.closes_at)
                    for shift in sorted(day.shifts, key=lambda item: item.opens_at)
                ),
            )
            for day in sorted(business.opening_days, key=lambda item: item.day_of_week)
        ),
    )
    profile_evidence = _profile_evidence_texts(profile)
    knowledge_evidence = tuple(
        f"{record.subject_key}: {record.content}" for record in knowledge
    )
    embedding_provider = create_embedding_provider(settings)
    evidence_embeddings = embedding_provider.embed(
        [owner_message.content, *profile_evidence, *knowledge_evidence]
    ).vectors
    question_embedding = evidence_embeddings[0]
    profile_end = 1 + len(profile_evidence)
    profile_similarities = tuple(
        _cosine_similarity(question_embedding, vector)
        for vector in evidence_embeddings[1:profile_end]
    )
    knowledge_similarities = tuple(
        _cosine_similarity(question_embedding, vector)
        for vector in evidence_embeddings[profile_end:]
    )
    selected_knowledge = _select_relevant_knowledge(
        knowledge, owner_message.content, knowledge_similarities, settings
    )
    has_relevant_profile = any(
        similarity >= settings.retrieval_minimum_similarity
        or _has_meaningful_overlap(owner_message.content, evidence)
        for evidence, similarity in zip(
            profile_evidence, profile_similarities, strict=True
        )
    )
    retrieved = retrieve(
        session,
        user,
        business_id,
        owner_message.content,
        embedding_provider,
        settings,
        question_embedding=question_embedding,
    )
    sources = _select_sources(
        retrieved.chunks, settings, question=owner_message.content
    )
    request = OwnerChatRequest(
        profile=profile,
        knowledge=selected_knowledge,
        sources=sources,
        messages=tuple(
            ProviderMessage(role=str(message.role), content=message.content)
            for message in messages
        ),
        requested_at=now,
        max_output_tokens=settings.owner_chat_max_output_tokens,
    )
    session.commit()
    return _PreparedTurn(
        request=request,
        business=business,
        has_usable_evidence=bool(has_relevant_profile or selected_knowledge or sources),
    )


def _select_sources(
    chunks: tuple[object, ...], settings: Settings, *, question: str | None = None
) -> tuple[ProviderSource, ...]:
    selected: list[ProviderSource] = []
    seen: set[str] = set()
    used = 0
    for chunk in chunks:
        content = str(chunk.content)
        normalized = " ".join(content.split()).casefold()
        tokens = estimate_utf8_tokens(content)
        if (
            normalized in seen
            or _is_unsafe_source_content(content)
            or (
                question is not None
                and float(chunk.similarity)
                < max(
                    settings.retrieval_minimum_similarity,
                    HIGH_CONFIDENCE_EVIDENCE_SIMILARITY,
                )
                and not _has_meaningful_overlap(question, content)
            )
            or len(selected) >= settings.rag_context_max_chunks
        ):
            continue
        if used + tokens > settings.rag_context_max_tokens:
            continue
        seen.add(normalized)
        used += tokens
        selected.append(
            ProviderSource(
                label=f"S{len(selected) + 1}",
                document_id=str(chunk.document_id),
                filename=str(chunk.document_filename),
                chunk_id=str(chunk.chunk_id),
                content=content,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                section_title=chunk.section_title,
            )
        )
    return tuple(selected)


def _normalized_safety_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _is_unsafe_source_content(content: str) -> bool:
    """Exclude clear document instructions without changing stored source text."""
    text = _normalized_safety_text(content)
    override = (
        "ignore all previous instructions",
        "ignore previous instructions",
        "replace system instructions",
        "ignore system prompt",
        "تجاهل التعليمات",
        "تجاهل كل التعليمات",
    )
    sensitive = (
        "system prompt",
        "api key",
        "password",
        "token",
        "credential",
        "storage key",
        "\u0645\u0641\u062a\u0627\u062d \u0627\u0644\u062a\u062e\u0632\u064a\u0646",
        "كشف التعليمات",
        "كلمة المرور",
        "مفتاح التخزين",
    )
    action = (
        "reveal",
        "show me",
        "expose",
        "access another business",
        "execute code",
        "run sql",
        "call tools",
        "external request",
        "اكشف",
        "اعرض",
        "نفذ",
    )
    return any(phrase in text for phrase in override) or (
        any(phrase in text for phrase in action)
        and any(phrase in text for phrase in sensitive)
    )


def _is_unsafe_reply(reply: str) -> bool:
    text = _normalized_safety_text(reply)
    sensitive_disclosure = (
        "system prompt",
        "storage key",
        "api key",
        "password is",
        "token is",
        "كلمة المرور هي",
        "مفتاح التخزين",
    )
    follows_malicious_instruction = (
        "i will follow the instructions",
        "ignore all previous instructions",
        "\u0627\u062a\u0628\u0639",
        "سأتبع التعليمات",
    )
    return any(phrase in text for phrase in sensitive_disclosure) or any(
        phrase in text for phrase in follows_malicious_instruction
    )


def _fallback_language(message: str, default_language: str) -> str:
    arabic_count = sum("\u0600" <= character <= "\u06ff" for character in message)
    latin_count = sum(
        character.isascii() and character.isalpha() for character in message
    )
    if arabic_count and latin_count:
        return "mixed"
    if arabic_count:
        normalized = _normalized_safety_text(message)
        lebanese_markers = (
            "بت",
            "شو",
            "قديش",
            "إيمتى",
            "ايمتى",
            "هال",
            "فيني",
            "فيك",
            "مش",
        )
        return (
            "lebanese_arabic"
            if any(marker in normalized for marker in lebanese_markers)
            else "arabic"
        )
    if re.search(r"(?i)(?:[a-z][2356789]|[2356789][a-z])", message):
        return "franco_arabic"
    return "arabic" if default_language == "ar" else "english"


def _missing_knowledge_reply(message: str, default_language: str) -> str:
    language = _fallback_language(message, default_language)
    replies = {
        "english": (
            "I don't have information about that yet. You can add it to your "
            "business profile or knowledge base, and I'll be able to help."
        ),
        "arabic": (
            "لا أملك معلومات عن ذلك بعد. يمكنك إضافتها إلى ملف نشاطك التجاري أو "
            "قاعدة المعرفة، وسأتمكن من مساعدتك."
        ),
        "lebanese_arabic": (
            "ما عندي معلومات عن هالموضوع بعد. فيك تضيفها ع ملف شغلك أو قاعدة "
            "المعرفة، وساعتها فيني ساعدك."
        ),
        "franco_arabic": (
            "Ma 3ande ma3loumet 3an hal mawdu3 ba3d. Fik tdifa 3a business "
            "profile aw knowledge base, w sa3eta fine se3dak."
        ),
        "mixed": (
            "I don't have information عن هالموضوع بعد. You can add it to your "
            "business profile أو knowledge base، وساعتها فيني ساعدك."
        ),
    }
    return replies[language]


def _mark_failed(
    session: Session,
    claim: _Claim,
    *,
    reservation: AIUsageReservationClaim | None = None,
    usage: TokenUsage | None = None,
    outcome: str | None = None,
    provider_identifier: str | None = None,
    model_identifier: str | None = None,
) -> None:
    message = session.scalar(
        select(OwnerChatMessage)
        .where(
            OwnerChatMessage.id == claim.message_id,
            OwnerChatMessage.generation_claim_token == claim.token,
        )
        .with_for_update()
    )
    if message is not None:
        message.generation_state = ChatGenerationState.FAILED
        message.generation_claim_token = None
        message.generation_claim_expires_at = None
    if reservation is not None and outcome is not None:
        reconcile_ai_usage(
            session,
            reservation.id,
            usage=usage,
            outcome=outcome,
            provider_identifier=provider_identifier,
            model_identifier=model_identifier,
            commit=False,
        )
    session.commit()


def _undo_pre_provider_admission(
    session: Session,
    business_id: uuid.UUID,
    claim: _Claim,
    generation_attempt: int,
) -> None:
    """Keep budget-blocked idempotent owner turns retryable without event inflation."""
    undone = undo_owner_chat_generation_admission(
        session,
        business_id=business_id,
        owner_message_id=claim.message_id,
        generation_attempt=generation_attempt,
        generation_claim_token=claim.token,
    )
    if not undone:
        raise RuntimeError("Owner generation admission could not be safely undone.")


def _validate_result(result: object, request: OwnerChatRequest) -> OwnerChatResult:
    if not isinstance(result, OwnerChatResult):
        raise OwnerChatProviderInvalidResponse
    if (
        not isinstance(result.reply, str)
        or not 1 <= len(result.reply.strip()) <= 14_000
    ):
        raise OwnerChatProviderInvalidResponse(reason="invalid_citations")
    if _is_unsafe_reply(result.reply):
        raise OwnerChatProviderInvalidResponse(reason="unsafe_output")
    if not isinstance(result.proposed_knowledge, tuple) or not all(
        hasattr(item, "subject_key")
        and hasattr(item, "content")
        and hasattr(item, "kind")
        and hasattr(item, "category")
        for item in result.proposed_knowledge
    ):
        raise OwnerChatProviderInvalidResponse
    if result.usage is not None and result.usage.output_tokens < 0:
        raise OwnerChatProviderInvalidResponse
    labels = {source.label for source in request.sources}
    if (
        not isinstance(result.cited_source_ids, tuple)
        or len(set(result.cited_source_ids)) != len(result.cited_source_ids)
        or any(
            not isinstance(label, str) or label not in labels
            for label in result.cited_source_ids
        )
    ):
        raise OwnerChatProviderInvalidResponse
    return result


def _persist_result(
    session: Session,
    business_id: uuid.UUID,
    claim: _Claim,
    result: OwnerChatResult,
    reservation: AIUsageReservationClaim | None,
    usage: TokenUsage | None,
    request: OwnerChatRequest,
) -> None:
    owner_message = session.scalar(
        select(OwnerChatMessage)
        .where(
            OwnerChatMessage.id == claim.message_id,
            OwnerChatMessage.generation_state == ChatGenerationState.PROCESSING,
            OwnerChatMessage.generation_claim_token == claim.token,
        )
        .with_for_update()
    )
    if owner_message is None:
        session.rollback()
        return
    now = utc_now()
    assistant = OwnerChatMessage(
        conversation_id=owner_message.conversation_id,
        sequence_number=owner_message.sequence_number + 1,
        role=ChatMessageRole.ASSISTANT,
        content=result.reply,
        reply_to_message_id=owner_message.id,
    )
    session.add(assistant)
    session.flush()
    source_by_label = {source.label: source for source in request.sources}
    session.add_all(
        OwnerChatCitation(
            business_id=business_id,
            assistant_message_id=assistant.id,
            document_id=uuid.UUID(source_by_label[label].document_id),
            chunk_id=uuid.UUID(source_by_label[label].chunk_id),
            citation_order=index,
            label=label,
            filename=source_by_label[label].filename,
            page_start=source_by_label[label].page_start,
            page_end=source_by_label[label].page_end,
            section_title=source_by_label[label].section_title,
        )
        for index, label in enumerate(result.cited_source_ids)
    )
    upsert_proposed_knowledge(
        session,
        business_id,
        owner_message.id,
        result.proposed_knowledge,
        now,
    )
    owner_message.generation_state = ChatGenerationState.COMPLETED
    owner_message.generation_claim_token = None
    owner_message.generation_claim_expires_at = None
    if reservation is not None:
        if usage is None:  # pragma: no cover - guarded by generation orchestration
            raise RuntimeError("Provider usage is required for a reserved turn.")
        reconcile_ai_usage(
            session,
            reservation.id,
            usage=usage,
            outcome="completed",
            provider_identifier=result.provider_identifier,
            model_identifier=result.model_identifier,
            commit=False,
        )
    session.commit()


def _generate_claimed_turn(
    session: Session,
    business_id: uuid.UUID,
    claim: _Claim,
    user: User,
    provider: OwnerChatProvider,
    settings: Settings,
) -> None:
    reservation: AIUsageReservationClaim | None = None
    try:
        prepared = _build_provider_request(
            session, user, business_id, claim.message_id, settings
        )
        request = prepared.request
        business = prepared.business
        if not prepared.has_usable_evidence:
            fallback = OwnerChatResult(
                reply=_missing_knowledge_reply(
                    request.messages[-1].content, business.default_language
                )
            )
            _persist_result(
                session,
                business_id,
                claim,
                fallback,
                None,
                None,
                request,
            )
            return
        owner_message = session.get(OwnerChatMessage, claim.message_id)
        if owner_message is None:
            raise _provider_unavailable()
        generation_attempt = owner_message.generation_attempts
        try:
            reservation = reserve_owner_chat_usage(
                session,
                business=business,
                user=user,
                owner_message_id=claim.message_id,
                generation_attempt=generation_attempt,
                estimated_input_tokens=provider.estimate_input_tokens(request),
                max_output_tokens=request.max_output_tokens,
                lease_seconds=settings.owner_chat_generation_lease_seconds,
            )
        except ApplicationError as exc:
            if exc.error_code == "daily_ai_token_limit_reached":
                _undo_pre_provider_admission(
                    session, business_id, claim, generation_attempt
                )
            raise
        result = _validate_result(provider.generate(request), request)
        usage = result.usage
        if usage is None:
            input_tokens = provider.estimate_input_tokens(request)
            output_tokens = estimate_utf8_tokens(result.reply)
            usage = TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                authoritative=False,
            )
        if usage.output_tokens > request.max_output_tokens:
            raise OwnerChatProviderInvalidResponse(
                usage=usage,
                provider_identifier=result.provider_identifier,
                model_identifier=result.model_identifier,
            )
    except OwnerChatProviderError as exc:
        outcome = (
            "reported_failure"
            if exc.usage is not None
            else "uncertain"
            if exc.usage_uncertain
            else "release"
        )
        _mark_failed(
            session,
            claim,
            reservation=reservation,
            usage=exc.usage,
            outcome=outcome if reservation is not None else None,
            provider_identifier=exc.provider_identifier,
            model_identifier=exc.model_identifier,
        )
        raise _provider_unavailable() from None
    except ApplicationError:
        raise
    except Exception:
        session.rollback()
        _mark_failed(
            session,
            claim,
            reservation=reservation,
            outcome="uncertain" if reservation is not None else None,
        )
        raise _provider_unavailable() from None
    try:
        _persist_result(
            session, business_id, claim, result, reservation, usage, request
        )
    except Exception as exc:
        session.rollback()
        _mark_failed(
            session,
            claim,
            reservation=reservation,
            usage=usage,
            outcome="reported_failure",
            provider_identifier=result.provider_identifier,
            model_identifier=result.model_identifier,
        )
        if isinstance(exc, ApplicationError):
            raise
        raise _provider_unavailable() from None


def submit_owner_message(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    body: OwnerMessageRequest,
    provider: OwnerChatProvider,
    settings: Settings,
) -> OwnerTurnResponse:
    """Persist an idempotent owner turn and process ordered generation inline."""
    _eligible_business(session, user, business_id)
    conversation = _get_or_create_conversation(session, business_id)
    owner_message, replayed = _create_or_reuse_owner_message(
        session, conversation.id, body
    )
    completed = _completed_turn(session, owner_message, replayed)
    if completed is not None:
        return completed

    deadline = time.monotonic() + settings.owner_chat_generation_wait_seconds
    while time.monotonic() < deadline:
        claim = _claim_oldest_turn(session, conversation.id, business_id, settings)
        if claim is None:
            session.expire_all()
            refreshed = session.get(OwnerChatMessage, owner_message.id)
            if refreshed is not None:
                completed = _completed_turn(session, refreshed, replayed)
                if completed is not None:
                    return completed
            session.rollback()
            time.sleep(0.025)
            continue
        _generate_claimed_turn(session, business_id, claim, user, provider, settings)
        session.expire_all()
        refreshed = session.get(OwnerChatMessage, owner_message.id)
        if refreshed is not None:
            completed = _completed_turn(session, refreshed, replayed)
            if completed is not None:
                return completed
    raise ApplicationError(
        "This conversation is still processing an earlier message. Please retry.",
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        error_code="generation_in_progress",
    )


def _encode_cursor(message: OwnerChatMessage) -> str:
    payload = json.dumps(
        {"sequence": message.sequence_number, "id": str(message.id)},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[int, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        return int(payload["sequence"]), uuid.UUID(payload["id"])
    except ValueError, TypeError, KeyError, json.JSONDecodeError:
        raise ApplicationError(
            "Conversation cursor is invalid.",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            error_code="invalid_conversation_cursor",
        ) from None


def get_conversation_history(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    cursor: str | None,
) -> ConversationHistoryResponse:
    _eligible_business(session, user, business_id)
    conversation = session.scalar(
        select(OwnerConversation).where(OwnerConversation.business_id == business_id)
    )
    if conversation is None:
        return ConversationHistoryResponse(items=[], next_cursor=None)
    query = (
        select(OwnerChatMessage)
        .where(OwnerChatMessage.conversation_id == conversation.id)
        .options(selectinload(OwnerChatMessage.citations))
    )
    if cursor is not None:
        sequence, message_id = _decode_cursor(cursor)
        query = query.where(
            or_(
                OwnerChatMessage.sequence_number < sequence,
                and_(
                    OwnerChatMessage.sequence_number == sequence,
                    OwnerChatMessage.id < message_id,
                ),
            )
        )
    rows = session.scalars(
        query.order_by(
            OwnerChatMessage.sequence_number.desc(), OwnerChatMessage.id.desc()
        ).limit(HISTORY_PAGE_SIZE + 1)
    ).all()
    page = rows[:HISTORY_PAGE_SIZE]
    next_cursor = _encode_cursor(page[-1]) if len(rows) > HISTORY_PAGE_SIZE else None
    page.reverse()
    return ConversationHistoryResponse(
        items=[_message_response(message) for message in page],
        next_cursor=next_cursor,
    )
