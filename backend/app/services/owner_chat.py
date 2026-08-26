"""Persistent, ordered owner-chat orchestration."""

from __future__ import annotations

import base64
import json
import logging
import math
import re
import time
import traceback
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import status
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.agent.owner_chat_provider import (
    OwnerChatProvider,
    OwnerChatProviderError,
    OwnerChatProviderInvalidResponse,
    OwnerChatProviderTimeout,
    OwnerChatProviderUnavailable,
    OwnerChatRequest,
    OwnerChatResult,
    ProviderBusinessProfile,
    ProviderCategoryCandidate,
    ProviderKnowledge,
    ProviderMessage,
    ProviderSource,
    ProviderToolDefinition,
    ProviderToolResult,
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
    OwnerConversationSummary,
    User,
)
from app.integrations.profiles import ConnectionProfileRegistry
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
from app.services.conversations import get_default_conversation, load_conversation
from app.tools.operational import (
    BEST_SELLING_PRODUCTS_TOOL,
    CURRENT_INVENTORY_TOOL,
    RESTOCKING_RECOMMENDATIONS_TOOL,
    SALES_SUMMARY_TOOL,
    OperationalToolExecutor,
    ToolExecutionError,
)

_logger = logging.getLogger(__name__)

CHAT_CONTEXT_MESSAGE_LIMIT = 12
MAX_OPERATIONAL_TOOL_EXECUTIONS = 2
MAX_OPERATIONAL_PROVIDER_CALLS = 3
HISTORY_PAGE_SIZE = 50
ABANDONED_TURN_RECOVERY_BATCH_SIZE = 100
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


def _add_usage(current: TokenUsage | None, added: TokenUsage) -> TokenUsage:
    if current is None:
        return added
    return TokenUsage(
        input_tokens=current.input_tokens + added.input_tokens,
        output_tokens=current.output_tokens + added.output_tokens,
        total_tokens=current.total_tokens + added.total_tokens,
        authoritative=current.authoritative and added.authoritative,
    )


def _provider_unavailable() -> ApplicationError:
    return ApplicationError(
        "The assistant is temporarily unavailable. Please retry.",
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        error_code="assistant_unavailable",
    )


def _safe_provider_failure(exc: OwnerChatProviderError) -> ApplicationError:
    if isinstance(exc, OwnerChatProviderTimeout):
        message = "The assistant took too long to respond. Please try again."
        error_code = "assistant_timeout"
    elif isinstance(exc, OwnerChatProviderInvalidResponse):
        message = "The assistant returned an unusable response. Please try again."
        error_code = "assistant_invalid_response"
    elif isinstance(exc, OwnerChatProviderUnavailable) and exc.reason == "rate_limited":
        message = (
            "The assistant is handling too many requests right now. "
            "Please try again later."
        )
        error_code = "assistant_rate_limited"
    else:
        message = "The assistant cannot be reached right now. Please try again."
        error_code = "assistant_transport_failure"
    return ApplicationError(
        message,
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        error_code=error_code,
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


def _conversation_busy() -> ApplicationError:
    return ApplicationError(
        "This conversation is already processing a message. Please retry shortly.",
        status_code=status.HTTP_409_CONFLICT,
        error_code="conversation_busy",
    )


def _owner_turn_failed() -> ApplicationError:
    return ApplicationError(
        "This message could not be completed. Send a new message to try again.",
        status_code=status.HTTP_409_CONFLICT,
        error_code="owner_turn_failed",
    )


def _fail_abandoned_turns(
    session: Session,
    conversation_id: uuid.UUID,
    *,
    now: datetime,
    stale_before: datetime,
) -> None:
    abandoned = session.scalars(
        select(OwnerChatMessage)
        .where(
            OwnerChatMessage.conversation_id == conversation_id,
            OwnerChatMessage.role == ChatMessageRole.OWNER,
            or_(
                and_(
                    OwnerChatMessage.generation_state == ChatGenerationState.PENDING,
                    OwnerChatMessage.created_at <= stale_before,
                ),
                and_(
                    OwnerChatMessage.generation_state == ChatGenerationState.PROCESSING,
                    OwnerChatMessage.generation_claim_expires_at <= now,
                ),
            ),
        )
        .order_by(OwnerChatMessage.sequence_number, OwnerChatMessage.id)
        .limit(ABANDONED_TURN_RECOVERY_BATCH_SIZE)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()
    for message in abandoned:
        message.generation_state = ChatGenerationState.FAILED
        message.generation_claim_token = None
        message.generation_claim_expires_at = None


def _create_or_reuse_owner_message(
    session: Session,
    conversation_id: uuid.UUID,
    body: OwnerMessageRequest,
    settings: Settings,
) -> tuple[OwnerChatMessage, bool, _Claim | None]:
    conversation = session.scalar(
        select(OwnerConversation)
        .where(OwnerConversation.id == conversation_id)
        .with_for_update()
    )
    if conversation is None:  # pragma: no cover - business owns the conversation
        raise _provider_unavailable()
    if conversation.archived:
        raise ApplicationError(
            "Archived conversations cannot receive new messages.",
            status_code=status.HTTP_409_CONFLICT,
            error_code="conversation_archived",
        )
    now = utc_now()
    _fail_abandoned_turns(
        session,
        conversation_id,
        now=now,
        stale_before=now
        - timedelta(seconds=settings.owner_chat_generation_lease_seconds),
    )
    existing = session.scalar(
        select(OwnerChatMessage)
        .where(
            OwnerChatMessage.conversation_id == conversation_id,
            OwnerChatMessage.idempotency_key == body.idempotency_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    replayed = existing is not None
    if existing is not None:
        if existing.content != body.content:
            session.rollback()
            raise ApplicationError(
                "This idempotency key was already used with different content.",
                status_code=status.HTTP_409_CONFLICT,
                error_code="idempotency_conflict",
            )
        if existing.generation_state in {
            ChatGenerationState.COMPLETED,
            ChatGenerationState.FAILED,
        }:
            session.commit()
            return existing, True, None
        if existing.generation_state == ChatGenerationState.PROCESSING:
            session.commit()
            return existing, True, None
    else:
        active_claim = session.scalar(
            select(OwnerChatMessage.id)
            .where(
                OwnerChatMessage.conversation_id == conversation_id,
                OwnerChatMessage.role == ChatMessageRole.OWNER,
                OwnerChatMessage.generation_state == ChatGenerationState.PROCESSING,
                OwnerChatMessage.generation_claim_expires_at > now,
            )
            .limit(1)
        )
        if active_claim is not None:
            session.commit()
            raise _conversation_busy()

        turn_number = conversation.next_turn_number
        conversation.next_turn_number += 1
        clean_title = " ".join(body.content.split())[:120]
        if conversation.title == "New conversation" and clean_title:
            conversation.title = clean_title
        conversation.last_message_at = now
        existing = OwnerChatMessage(
            conversation_id=conversation.id,
            sequence_number=turn_number * 2 - 1,
            role=ChatMessageRole.OWNER,
            content=body.content,
            idempotency_key=body.idempotency_key,
            generation_state=ChatGenerationState.PENDING,
        )
        session.add(existing)
        session.flush()

    token = uuid.uuid4()
    existing.generation_state = ChatGenerationState.PROCESSING
    existing.generation_claim_token = token
    existing.generation_claim_expires_at = now + timedelta(
        seconds=settings.owner_chat_generation_lease_seconds
    )
    message_id = existing.id
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
        return raced, True, None
    return existing, replayed, _Claim(message_id=message_id, token=token)


def _message_response(message: OwnerChatMessage) -> ChatMessageResponse:
    return ChatMessageResponse(
        id=message.id,
        sequence_number=message.sequence_number,
        role=message.role,
        content=message.content,
        created_at=message.created_at,
        reply_to_message_id=message.reply_to_message_id,
        generation_state=message.generation_state,
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


def _admit_provider_generation(
    session: Session,
    business_id: uuid.UUID,
    claim: _Claim,
) -> int:
    message = session.scalar(
        select(OwnerChatMessage)
        .where(
            OwnerChatMessage.id == claim.message_id,
            OwnerChatMessage.generation_state == ChatGenerationState.PROCESSING,
            OwnerChatMessage.generation_claim_token == claim.token,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if message is None:
        session.rollback()
        raise _conversation_busy()
    next_attempt = message.generation_attempts + 1
    admit_owner_chat_generation(
        session,
        business_id=business_id,
        owner_message_id=message.id,
        generation_attempt=next_attempt,
    )
    message.generation_attempts = next_attempt
    session.commit()
    return next_attempt


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


def _normalized_classifier_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    return " ".join(
        "".join(
            character
            for character in normalized
            if not unicodedata.combining(character)
        ).split()
    )


def _is_product_quantity_request(value: str) -> bool:
    """Recognize bounded stock phrasing without classifying generic quantities."""

    text = _normalized_classifier_text(value).strip(" ?!.,")
    if not text:
        return False
    patterns = (
        r"\bhow many (?P<product>.+?) do we have(?: left| remaining)?\b",
        r"\bdo we have (?P<product>.+?) (?:available|left|in stock)\b",
        r"\bwhat is (?:the )?quantity of (?P<product>.+?)$",
        r"\bhow much (?P<product>.+?) (?:remains?|is left)\b",
        r"(?:قديش|كم)\s+(?:عنا\s+)?(?P<product>.+?)(?:\s+(?:باقي|ضال|ضل))?$",
        r"هل\s+(?:المنتج\s+)?(?P<product>.+?)\s+متوفر$",
        r"قديش\s+باقي\s+من\s+هيدا\s+المنتج$",
        r"\b(?:adde|addeh|kam)\s+(?:3anna\s+)?(?P<product>.+?)(?:\s+ba2e)?$",
        r"\bfi\s+(?P<product>.+?)\s+available$",
        r"\badde\s+ba2e\s+men\s+(?P<product>.+?)$",
    )
    non_product_terms = {
        "day",
        "days",
        "hour",
        "hours",
        "time",
        "people",
        "person",
        "employee",
        "employees",
        "customer",
        "customers",
        "order",
        "orders",
        "appointment",
        "appointments",
        "meeting",
        "meetings",
        "يوم",
        "ايام",
        "ساعة",
        "ساعات",
        "موظف",
        "موظفين",
        "زبون",
        "زباين",
        "طلبات",
        "مواعيد",
    }
    for pattern in patterns:
        match = re.search(pattern, text)
        if match is None:
            continue
        product = match.groupdict().get("product")
        if product is None:
            return True
        product_terms = set(re.findall(r"[^\W_]+", product, flags=re.UNICODE))
        return bool(product_terms) and not product_terms <= non_product_terms
    return False


def _query_concepts(value: str) -> frozenset[str]:
    text = _normalized_classifier_text(value)
    patterns = {
        "returns": (
            r"\breturn\w*\b",
            r"\brefund\w*\b",
            r"\bexchange\w*\b",
            r"ارجاع",
            r"ترجيع",
            r"رجع",
            r"\b(?:tarji\w*|raje3\w*|rja3\w*)\b",
        ),
        "delivery": (
            r"\bdeliver\w*\b",
            r"\bshipping\b",
            r"توصيل",
            r"شحن",
            r"\b(?:tawsil|tawsiil|sh7n)\b",
        ),
        "warranty": (
            r"\bwarrant\w*\b",
            r"\bguarantee\w*\b",
            r"ضمان",
            r"\bdaman\b",
        ),
        "opening_hours": (
            r"\bopening\s+hours\b",
            r"\b(?:open|close|hours|schedule)\b",
            r"ساعات العمل",
            r"دوام",
            r"فتح",
            r"سكر",
            r"\b(?:wa2et|fte7|fta7|btefta\w*|bteskar\w*)\b",
        ),
        "location": (
            r"\b(?:address|location|located|where)\b",
            r"عنوان",
            r"موقع",
            r"وين",
            r"\b(?:wen|wein)\b",
        ),
        "inventory": (
            r"\b(?:inventory|stock)\b",
            r"مخزون",
            r"\bmakhzou?n\b",
        ),
        "sales": (
            r"\b(?:sales?|selling|sellers?|sold)\b",
            r"مبيعات",
            r"مبيعا",
            r"\bmabi3\w*\b",
        ),
        "orders": (
            r"\borders?\b",
            r"طلبات",
            r"طلبي",
            r"\btalabiy\w*\b",
        ),
        "revenue": (
            r"\b(?:revenue|turnover|profit|earnings)\b",
            r"ايراد",
            r"ارباح",
            r"\b(?:iradet|eradet|arbe7)\b",
        ),
        "restocking": (
            r"\b(?:restock\w*|replenish\w*)\b",
            r"اعادة تخزين",
            r"تزويد المخزون",
            r"\b(?:restock|ta3biye)\b",
        ),
        "appointments": (
            r"\b(?:appointment|booking)s?\b",
            r"مواعيد",
            r"حجوزات",
            r"\bmawa3id\b",
        ),
    }
    concepts = {
        concept
        for concept, concept_patterns in patterns.items()
        if any(re.search(pattern, text) for pattern in concept_patterns)
    }
    if _is_product_quantity_request(value):
        concepts.add("inventory")
    return frozenset(concepts)


def _search_query_text(value: str) -> str:
    search_terms = {
        "returns": "return refund exchange policy",
        "delivery": "delivery shipping policy",
        "warranty": "warranty guarantee policy",
        "opening_hours": "business opening hours schedule",
        "location": "business address location",
        "inventory": "current inventory stock availability",
        "sales": "current sales best selling items",
        "orders": "current customer orders",
        "revenue": "current revenue earnings",
        "restocking": "current restocking replenishment",
        "appointments": "current appointment booking availability",
    }
    concepts = _query_concepts(value)
    expanded = " ".join(search_terms[concept] for concept in sorted(concepts))
    return value if not expanded else f"{value}\nSearch concepts: {expanded}"


def _is_general_conversation_request(value: str) -> bool:
    concepts = _query_concepts(value)
    if concepts - {
        "inventory",
        "sales",
        "orders",
        "revenue",
        "restocking",
        "appointments",
    }:
        return False
    text = _normalized_classifier_text(value)
    patterns = (
        r"\bhow (?:can|could|do|should) (?:i|we)\b",
        r"\b(?:give|offer) me (?:advice|tips|ideas)\b",
        r"\b(?:brainstorm|explain|motivate|summarize)\b",
        r"\b(?:tell|write) me (?:a |some )?(?:joke|story|ideas?)\b",
        r"\bwhat do you think about\b",
        r"(?:كيف فيني|كيف يمكنني|كيف فينا|شو بتنصح|اعطني نصائح|أعطني نصائح|"
        r"نصائح|افكار|أفكار|اشرح|فسر)",
        r"\b(?:kif fini|kif fine|kif fina|shu btensa7|nasi7a|nase7a|afkar|"
        r"brainstorm|explain)\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _is_live_operational_request(value: str) -> bool:
    operational = {
        "inventory",
        "sales",
        "orders",
        "revenue",
        "restocking",
        "appointments",
    }
    concepts = _query_concepts(value) & operational
    if not concepts:
        return False
    text = _normalized_classifier_text(value)
    explicit_live = (
        r"\b(?:current|today|tonight|now|latest|live|this (?:day|week|month)|"
        r"how many|how much|best sell\w*|top sell\w*|in stock|available now)\b",
        r"(?:الحالي|الحالية|اليوم|هلق|الان|الآن|قديش|كم|الأكثر مبيعا|"
        r"الاكثر مبيعا|متوفر حاليا)",
        r"\b(?:el yom|lyom|halla2|hala2|adde|addeh|kam|current|latest|"
        r"in stock|available)\b",
    )
    if any(re.search(pattern, text) for pattern in explicit_live):
        return True
    advice_markers = (
        r"\b(?:advice|tips|ideas|strategy|strategies|plan|planning|manage|"
        r"management|improve|increase|explain)\b",
        r"(?:نصيحة|نصائح|افكار|أفكار|استراتيجية|خطة|ادارة|إدارة|تحسين|اشرح)",
        r"\b(?:nasi7a|nase7a|afkar|strategy|plan|idara|ta7sin)\b",
    )
    if _is_general_conversation_request(value) and any(
        re.search(pattern, text) for pattern in advice_markers
    ):
        return False
    return True


def _matching_operational_tools(value: str) -> frozenset[str]:
    """Map deterministic message concepts to the smallest approved tool set."""

    concepts = _query_concepts(value)
    names: set[str] = set()
    if "inventory" in concepts:
        names.add(CURRENT_INVENTORY_TOOL)
    if concepts & {"sales", "revenue"}:
        names.add(SALES_SUMMARY_TOOL)
    if "sales" in concepts:
        names.add(BEST_SELLING_PRODUCTS_TOOL)
    if "restocking" in concepts:
        names.add(RESTOCKING_RECOMMENDATIONS_TOOL)
    return frozenset(names)


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


def _provider_profile(business: Business) -> ProviderBusinessProfile:
    return ProviderBusinessProfile(
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


def _provider_context(
    session: Session, owner_message: OwnerChatMessage
) -> tuple[str | None, tuple[ProviderMessage, ...]]:
    summary = session.scalar(
        select(OwnerConversationSummary).where(
            OwnerConversationSummary.conversation_id == owner_message.conversation_id,
            OwnerConversationSummary.summary_version > 0,
        )
    )
    checkpoint = (
        summary.summarized_through_sequence_number if summary is not None else 0
    )
    prior = session.scalars(
        select(OwnerChatMessage)
        .where(
            OwnerChatMessage.conversation_id == owner_message.conversation_id,
            OwnerChatMessage.sequence_number < owner_message.sequence_number,
            OwnerChatMessage.sequence_number > checkpoint,
            or_(
                OwnerChatMessage.role == ChatMessageRole.ASSISTANT,
                OwnerChatMessage.generation_state == ChatGenerationState.COMPLETED,
            ),
        )
        .order_by(OwnerChatMessage.sequence_number.desc(), OwnerChatMessage.id.desc())
        .limit(CHAT_CONTEXT_MESSAGE_LIMIT)
    ).all()
    prior.reverse()
    messages = [*prior, owner_message]
    return (summary.content if summary is not None else None), tuple(
        ProviderMessage(role=str(message.role), content=message.content)
        for message in messages
    )


def _load_provider_business(
    session: Session, business_id: uuid.UUID
) -> Business | None:
    return session.scalar(
        select(Business)
        .where(Business.id == business_id)
        .options(
            selectinload(Business.opening_days).selectinload(BusinessOpeningDay.shifts)
        )
        .execution_options(populate_existing=True)
    )


def _build_conversation_request(
    session: Session,
    business_id: uuid.UUID,
    owner_message_id: uuid.UUID,
    settings: Settings,
) -> _PreparedTurn:
    owner_message = session.get(OwnerChatMessage, owner_message_id)
    business = _load_provider_business(session, business_id)
    if owner_message is None or business is None:
        raise _provider_unavailable()
    rolling_summary, messages = _provider_context(session, owner_message)
    request = OwnerChatRequest(
        profile=_provider_profile(business),
        knowledge=(),
        sources=(),
        messages=messages,
        rolling_summary=rolling_summary,
        requested_at=utc_now(),
        max_output_tokens=settings.owner_chat_max_output_tokens,
        mode="conversation",
    )
    session.commit()
    return _PreparedTurn(request=request, business=business, has_usable_evidence=True)


def _build_operational_request(
    session: Session,
    business_id: uuid.UUID,
    owner_message_id: uuid.UUID,
    settings: Settings,
    definitions: tuple[ProviderToolDefinition, ...],
    results: tuple[ProviderToolResult, ...] = (),
    *,
    requested_at: datetime | None = None,
    category_candidates: tuple[ProviderCategoryCandidate, ...] = (),
) -> _PreparedTurn:
    owner_message = session.get(OwnerChatMessage, owner_message_id)
    business = _load_provider_business(session, business_id)
    if owner_message is None or business is None:
        raise _provider_unavailable()
    _rolling_summary, messages = _provider_context(session, owner_message)
    request = OwnerChatRequest(
        profile=_provider_profile(business),
        knowledge=(),
        sources=(),
        messages=messages,
        rolling_summary=None,
        requested_at=requested_at or utc_now(),
        max_output_tokens=settings.owner_chat_max_output_tokens,
        mode="operational",
        tools=definitions,
        tool_results=results,
        category_candidates=category_candidates,
    )
    session.commit()
    return _PreparedTurn(request=request, business=business, has_usable_evidence=True)


def _build_provider_request(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    owner_message_id: uuid.UUID,
    settings: Settings,
) -> _PreparedTurn:
    owner_message = session.get(OwnerChatMessage, owner_message_id)
    business = _load_provider_business(session, business_id)
    if owner_message is None or business is None:
        raise _provider_unavailable()
    rolling_summary, messages = _provider_context(session, owner_message)
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
    profile = _provider_profile(business)
    profile_evidence = _profile_evidence_texts(profile)
    knowledge_evidence = tuple(
        f"{record.subject_key}: {record.content}" for record in knowledge
    )
    search_query = _search_query_text(owner_message.content)
    embedding_provider = create_embedding_provider(settings)
    evidence_embeddings = embedding_provider.embed(
        [search_query, *profile_evidence, *knowledge_evidence]
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
        knowledge, search_query, knowledge_similarities, settings
    )
    has_relevant_profile = any(
        similarity >= settings.retrieval_minimum_similarity
        or _has_meaningful_overlap(search_query, evidence)
        for evidence, similarity in zip(
            profile_evidence, profile_similarities, strict=True
        )
    )
    retrieved = retrieve(
        session,
        user,
        business_id,
        search_query,
        embedding_provider,
        settings,
        question_embedding=question_embedding,
    )
    sources = _select_sources(retrieved.chunks, settings, question=search_query)
    request = OwnerChatRequest(
        profile=profile,
        knowledge=selected_knowledge,
        sources=sources,
        messages=messages,
        rolling_summary=rolling_summary,
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
            "كيفك",
            "أهلين",
            "مرسي",
            "تمام",
        )
        return (
            "lebanese_arabic"
            if any(marker in normalized for marker in lebanese_markers)
            else "arabic"
        )
    franco_markers = re.search(
        r"(?i)\b(?:shu|shou|wen|wein|emta|adde|addesh|kif|leish|lesh|"
        r"nhar|fina|fine|fik|bte[a-z]*|bt[a-z]+|hal|mawdu3|marhaba|"
        r"ahla|ahlein|merci|shukran|chokran|tamam|fhemet|kifak|kifik|"
        r"bshoufak|yalla)\b",
        message,
    )
    if re.search(r"(?i)(?:[a-z][2356789]|[2356789][a-z])", message) or franco_markers:
        return "franco_arabic"
    if latin_count:
        return "english"
    return "arabic" if default_language == "ar" else "english"


def _requires_business_evidence(value: str) -> bool:
    if _query_concepts(value):
        return True
    if _is_general_conversation_request(value):
        return False
    text = _normalized_classifier_text(value)
    patterns = (
        r"\b(?:our|my) (?:business|store|shop|company|products?|services?|"
        r"prices?|polic(?:y|ies)|hours|location|address)\b",
        r"\b(?:do|can) you (?:sell|offer|provide|repair|carry|deliver|accept|"
        r"stock|make)\b",
        r"\b(?:what|which) (?:products?|services?|payment methods?|"
        r"polic(?:y|ies))\b",
        r"\b(?:product|service|price|cost|payment|catalog|menu)\b",
        r"(?:عندكم|عنا|بتبيعوا|تبيعون|بتعملوا|بتقدموا|تقدمون|"
        r"محل(?:نا|كم)?|نشاط(?:نا|كم)?|خدمة|منتج|سعر|دفع|سياسة)",
        r"\b(?:3endkon|3anna|btbi3o|bta3mlo|bte?2addmo|ma7al|khedme|"
        r"muntaj|se3er|daf3|siyese|policy)\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


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


def _live_operational_reply(message: str, default_language: str) -> str:
    language = _fallback_language(message, default_language)
    replies = {
        "english": (
            "I can't access live operational data yet. Current inventory, sales, "
            "orders, revenue, appointments, and restocking require a connected "
            "operational tool."
        ),
        "arabic": (
            "لا تتوفر لدي بيانات تشغيلية مباشرة بعد. المخزون والمبيعات والطلبات "
            "والإيرادات والمواعيد وإعادة التخزين تحتاج إلى أداة تشغيلية موصولة."
        ),
        "lebanese_arabic": (
            "ما فيني وصّل للبيانات التشغيلية المباشرة بعد. المخزون والمبيعات "
            "والطلبات والمواعيد وإعادة التخزين بدها أداة تشغيلية موصولة."
        ),
        "franco_arabic": (
            "Ma fine ousal lal live operational data ba3d. El stock, sales, orders, "
            "revenue, appointments, w restocking baddon connected operational tool."
        ),
        "mixed": (
            "I can't access البيانات التشغيلية المباشرة yet. Current stock, sales, "
            "orders, revenue, appointments، وإعادة التخزين need a connected "
            "operational tool."
        ),
    }
    return replies[language]


def _conflicting_source_labels(
    sources: tuple[ProviderSource, ...],
) -> tuple[str, ...]:
    ignored = {
        "business",
        "current",
        "customer",
        "document",
        "information",
        "policy",
        "service",
        "that",
        "the",
        "this",
        "with",
    }

    def stated_values(content: str) -> set[str]:
        return set(re.findall(r"\b\d+(?:[.,]\d+)?%?\b", content))

    def has_negation(content: str) -> bool:
        text = _normalized_classifier_text(content)
        return bool(
            re.search(r"\b(?:no|not|never|without|ma|mesh|mish)\b", text)
            or re.search(r"(?:ليس|لا|غير)", text)
        )

    involved: set[str] = set()
    for index, left in enumerate(sources):
        left_terms = _evidence_terms(left.content) - ignored
        left_values = stated_values(left.content)
        for right in sources[index + 1 :]:
            right_terms = _evidence_terms(right.content) - ignored
            smaller = min(len(left_terms), len(right_terms))
            if smaller == 0 or len(left_terms & right_terms) / smaller < 0.6:
                continue
            right_values = stated_values(right.content)
            numeric_conflict = bool(
                left_values and right_values and left_values != right_values
            )
            polarity_conflict = has_negation(left.content) != has_negation(
                right.content
            )
            if numeric_conflict or polarity_conflict:
                involved.update((left.label, right.label))
    return tuple(source.label for source in sources if source.label in involved)


def _conflict_reply(
    message: str,
    default_language: str,
    sources: tuple[ProviderSource, ...] = (),
    labels: tuple[str, ...] = (),
) -> str:
    language = _fallback_language(message, default_language)
    involved = set(labels)
    values = tuple(
        dict.fromkeys(
            value
            for source in sources
            if not involved or source.label in involved
            for value in re.findall(r"\b\d+(?:[.,]\d+)?%?\b", source.content)
        )
    )
    value_text = " / ".join(values)
    details = {
        "english": f" The stated values are {value_text}." if value_text else "",
        "arabic": f" القيم المذكورة هي {value_text}." if value_text else "",
        "lebanese_arabic": f" القيم المذكورة هي {value_text}." if value_text else "",
        "franco_arabic": f" El values el mazkurin henne {value_text}."
        if value_text
        else "",
        "mixed": f" The stated values هي {value_text}." if value_text else "",
    }
    replies = {
        "english": (
            "I found conflicting information in the trusted sources."
            f"{details['english']} Please clarify which information is current or "
            "update the knowledge base."
        ),
        "arabic": (
            "وجدت معلومات متعارضة في المصادر الموثوقة."
            f"{details['arabic']} يرجى توضيح أي معلومات هي "
            "الحالية أو تحديث قاعدة المعرفة."
        ),
        "lebanese_arabic": (
            "لقيت معلومات متعارضة بالمصادر الموثوقة."
            f"{details['lebanese_arabic']} فيك توضّح أي معلومة هي "
            "الحالية أو تحدّث قاعدة المعرفة؟"
        ),
        "franco_arabic": (
            "La2et ma3loumet met3arda bel trusted sources."
            f"{details['franco_arabic']} Fik twaddi7 ayya "
            "ma3loume hiye el current aw t7addet el knowledge base?"
        ),
        "mixed": (
            "I found معلومات متعارضة in the trusted sources."
            f"{details['mixed']} Please وضّح أي "
            "معلومة هي current أو update the knowledge base."
        ),
    }
    return replies[language]


def _enforce_conflict_result(
    result: OwnerChatResult,
    message: str,
    default_language: str,
    sources: tuple[ProviderSource, ...],
    conflict_labels: tuple[str, ...],
) -> OwnerChatResult:
    text = _normalized_safety_text(result.reply)
    clarification_markers = (
        "clarif",
        "confirm which",
        "which information",
        "which policy",
        "وض",
        "شرح",
        "أي سياسة",
        "اي سياسة",
        "waddi7",
        "twaddi7",
        "2akked",
        "ayya siyese",
        "ayye siyese",
    )
    citations_are_complete = len(result.cited_source_ids) == len(
        conflict_labels
    ) and set(result.cited_source_ids) == set(conflict_labels)
    reply = (
        result.reply
        if citations_are_complete
        and any(marker in text for marker in clarification_markers)
        else _conflict_reply(
            message,
            default_language,
            sources,
            conflict_labels,
        )
    )
    return OwnerChatResult(
        reply=reply,
        cited_source_ids=conflict_labels,
        usage=result.usage,
        provider_identifier=result.provider_identifier,
        model_identifier=result.model_identifier,
    )


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
    """Remove admission when generation never reached the provider."""
    undone = undo_owner_chat_generation_admission(
        session,
        business_id=business_id,
        owner_message_id=claim.message_id,
        generation_attempt=generation_attempt,
        generation_claim_token=claim.token,
    )
    if not undone:
        raise RuntimeError("Owner generation admission could not be safely undone.")


def _usage_for_result(
    provider: OwnerChatProvider,
    request: OwnerChatRequest,
    result: OwnerChatResult,
) -> TokenUsage:
    if result.usage is not None:
        return result.usage
    output = (
        result.reply
        if result.decision != "tool"
        else json.dumps(
            {"tool_name": result.tool_name, "arguments": result.tool_arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    input_tokens = provider.estimate_input_tokens(request)
    output_tokens = estimate_utf8_tokens(output)
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        authoritative=False,
    )


def _operational_result_with_usage(
    result: OwnerChatResult, usage: TokenUsage
) -> OwnerChatResult:
    return OwnerChatResult(
        reply=result.reply,
        usage=usage,
        provider_identifier=result.provider_identifier,
        model_identifier=result.model_identifier,
        decision=result.decision,
    )


def _product_resolution_reply(
    tool_results: list[ProviderToolResult],
    message: str,
    default_language: str,
) -> str | None:
    if not tool_results:
        return None
    resolution = tool_results[-1].output.get("resolution")
    if not isinstance(resolution, dict):
        return None
    status = resolution.get("status")
    language = _fallback_language(message, default_language)
    if status == "not_found":
        replies = {
            "english": "I couldn't find that product in the live catalogue.",
            "arabic": "لم أجد هذا المنتج في الكتالوج المباشر.",
            "lebanese_arabic": "ما لقيت هيدا المنتج بالكاتالوغ المباشر.",
            "franco_arabic": "Ma la2et hal product bel live catalogue.",
            "mixed": "I couldn't find هيدا المنتج in the live catalogue.",
        }
        return replies[language]
    if status != "ambiguous":
        return None
    raw_candidates = resolution.get("candidates")
    if not isinstance(raw_candidates, list):
        return None
    labels: list[str] = []
    for candidate in raw_candidates[:5]:
        if not isinstance(candidate, dict) or not isinstance(
            candidate.get("name"), str
        ):
            continue
        sku = candidate.get("sku")
        labels.append(
            f"{candidate['name']} ({sku})"
            if isinstance(sku, str)
            else candidate["name"]
        )
    if not labels:
        return None
    candidate_text = "; ".join(labels)
    replies = {
        "english": (
            f"I found several matching products: {candidate_text}. "
            "Which one do you mean?"
        ),
        "arabic": f"وجدت عدة منتجات مطابقة: {candidate_text}. أي منتج تقصد؟",
        "lebanese_arabic": f"لقيت أكتر من منتج مطابق: {candidate_text}. أي واحد قصدك؟",
        "franco_arabic": (
            f"La2et aktar men product: {candidate_text}. Ayya wa7ad 2asdak?"
        ),
        "mixed": f"I found أكتر من منتج: {candidate_text}. Which one do you mean?",
    }
    return replies[language]


def _category_resolution_reply(
    tool_results: list[ProviderToolResult],
    message: str,
    default_language: str,
) -> str | None:
    if not tool_results:
        return None
    resolution = tool_results[-1].output.get("category_resolution")
    if not isinstance(resolution, dict) or resolution.get("status") != "ambiguous":
        return None
    candidates = resolution.get("candidates")
    if not isinstance(candidates, list):
        return None
    labels = tuple(
        candidate["label"]
        for candidate in candidates[:5]
        if isinstance(candidate, dict) and isinstance(candidate.get("label"), str)
    )
    if len(labels) < 2:
        return None
    candidate_text = "; ".join(labels)
    language = _fallback_language(message, default_language)
    replies = {
        "english": (
            f"I found several matching categories: {candidate_text}. "
            "Which one do you mean?"
        ),
        "arabic": f"وجدت عدة فئات مطابقة: {candidate_text}. أي فئة تقصد؟",
        "lebanese_arabic": f"لقيت أكتر من فئة مطابقة: {candidate_text}. أي فئة قصدك؟",
        "franco_arabic": (
            f"La2et aktar men category: {candidate_text}. Ayya wa7de 2asdak?"
        ),
        "mixed": f"I found أكتر من category: {candidate_text}. Which one do you mean?",
    }
    return replies[language]


def _run_operational_loop(
    session: Session,
    business_id: uuid.UUID,
    claim: _Claim,
    user: User,
    provider: OwnerChatProvider,
    settings: Settings,
    executor: OperationalToolExecutor,
    definitions: tuple[ProviderToolDefinition, ...],
    category_candidates: tuple[ProviderCategoryCandidate, ...] = (),
) -> tuple[OwnerChatResult, TokenUsage]:
    tool_results: list[ProviderToolResult] = []
    call_fingerprints: set[str] = set()
    aggregate_usage: TokenUsage | None = None
    requested_at = utc_now()

    for _provider_call in range(1, MAX_OPERATIONAL_PROVIDER_CALLS + 1):
        prepared = _build_operational_request(
            session,
            business_id,
            claim.message_id,
            settings,
            definitions,
            tuple(tool_results),
            requested_at=requested_at,
            category_candidates=category_candidates,
        )
        request = prepared.request
        try:
            result = _validate_result(provider.generate(request), request)
        except OwnerChatProviderError as exc:
            if aggregate_usage is not None and not exc.usage_uncertain:
                exc.usage = (
                    _add_usage(aggregate_usage, exc.usage)
                    if exc.usage is not None
                    else aggregate_usage
                )
            raise
        call_usage = _usage_for_result(provider, request, result)
        if call_usage.output_tokens > request.max_output_tokens:
            raise OwnerChatProviderInvalidResponse(
                usage=_add_usage(aggregate_usage, call_usage),
                provider_identifier=result.provider_identifier,
                model_identifier=result.model_identifier,
            )
        aggregate_usage = _add_usage(aggregate_usage, call_usage)

        resolution_reply = _product_resolution_reply(
            tool_results,
            request.messages[-1].content,
            prepared.business.default_language,
        )
        if resolution_reply is not None:
            resolved_result = OwnerChatResult(
                reply=resolution_reply,
                usage=aggregate_usage,
                provider_identifier=result.provider_identifier,
                model_identifier=result.model_identifier,
                decision="final",
            )
            return resolved_result, aggregate_usage
        category_reply = _category_resolution_reply(
            tool_results,
            request.messages[-1].content,
            prepared.business.default_language,
        )
        if category_reply is not None:
            resolved_result = OwnerChatResult(
                reply=category_reply,
                usage=aggregate_usage,
                provider_identifier=result.provider_identifier,
                model_identifier=result.model_identifier,
                decision="final",
            )
            return resolved_result, aggregate_usage

        if result.decision == "unavailable":
            return _operational_result_with_usage(
                result, aggregate_usage
            ), aggregate_usage
        if result.decision == "final":
            if not tool_results:
                unavailable = OwnerChatResult(
                    reply=_live_operational_reply(
                        request.messages[-1].content,
                        prepared.business.default_language,
                    ),
                    usage=aggregate_usage,
                    provider_identifier=result.provider_identifier,
                    model_identifier=result.model_identifier,
                    decision="unavailable",
                )
                return unavailable, aggregate_usage
            return _operational_result_with_usage(
                result, aggregate_usage
            ), aggregate_usage

        arguments = result.tool_arguments or {}
        fingerprint = json.dumps(
            {"name": result.tool_name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if (
            fingerprint in call_fingerprints
            or len(tool_results) >= MAX_OPERATIONAL_TOOL_EXECUTIONS
        ):
            try:
                executor.reject(
                    user=user,
                    business_id=business_id,
                    tool_name=result.tool_name,
                    arguments=arguments,
                    code="loop_limit",
                )
            except ToolExecutionError:
                fallback = OwnerChatResult(
                    reply=_live_operational_reply(
                        request.messages[-1].content,
                        prepared.business.default_language,
                    ),
                    usage=aggregate_usage,
                    provider_identifier=result.provider_identifier,
                    model_identifier=result.model_identifier,
                    decision="unavailable",
                )
                return fallback, aggregate_usage
        call_fingerprints.add(fingerprint)
        try:
            executed = executor.execute(
                user=user,
                business_id=business_id,
                tool_name=result.tool_name,
                arguments=arguments,
            )
        except ToolExecutionError:
            fallback = OwnerChatResult(
                reply=_live_operational_reply(
                    request.messages[-1].content,
                    prepared.business.default_language,
                ),
                usage=aggregate_usage,
                provider_identifier=result.provider_identifier,
                model_identifier=result.model_identifier,
                decision="unavailable",
            )
            return fallback, aggregate_usage
        tool_results.append(
            ProviderToolResult(
                tool_name=executed.tool_name,
                output=executed.output.model_dump(mode="json"),
            )
        )

    raise RuntimeError("Operational provider loop exited unexpectedly.")


def _validate_result(result: object, request: OwnerChatRequest) -> OwnerChatResult:
    if not isinstance(result, OwnerChatResult):
        raise OwnerChatProviderInvalidResponse
    if request.mode == "operational":
        if result.proposed_knowledge or result.cited_source_ids:
            raise OwnerChatProviderInvalidResponse(
                reason="invalid_operational_response"
            )
        if result.decision == "tool":
            if (
                result.reply
                or not isinstance(result.tool_name, str)
                or not isinstance(result.tool_arguments, dict)
            ):
                raise OwnerChatProviderInvalidResponse(
                    reason="invalid_operational_response"
                )
        elif result.decision in {"final", "unavailable"}:
            if (
                not isinstance(result.reply, str)
                or not 1 <= len(result.reply.strip()) <= 14_000
                or result.tool_name is not None
                or result.tool_arguments is not None
            ):
                raise OwnerChatProviderInvalidResponse(
                    reason="invalid_operational_response"
                )
            if _is_unsafe_reply(result.reply):
                raise OwnerChatProviderInvalidResponse(reason="unsafe_output")
        else:
            raise OwnerChatProviderInvalidResponse(
                reason="invalid_operational_response"
            )
    else:
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
    if not isinstance(result.requires_business_knowledge, bool):
        raise OwnerChatProviderInvalidResponse
    if request.mode == "conversation" and (
        result.proposed_knowledge or result.cited_source_ids
    ):
        raise OwnerChatProviderInvalidResponse(reason="invalid_conversation_response")
    if request.mode == "grounded" and result.requires_business_knowledge:
        raise OwnerChatProviderInvalidResponse(reason="invalid_grounded_response")
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
    sources: tuple[ProviderSource, ...],
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
    source_by_label = {source.label: source for source in sources}
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
    conversation = session.get(OwnerConversation, owner_message.conversation_id)
    if conversation is not None:
        conversation.last_message_at = now
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
    profiles: ConnectionProfileRegistry | None,
) -> bool:
    reservation: AIUsageReservationClaim | None = None
    aggregate_usage: TokenUsage | None = None
    try:
        owner_message = session.get(OwnerChatMessage, claim.message_id)
        business = session.get(Business, business_id)
        if owner_message is None or business is None:
            raise _provider_unavailable()
        # Operational intent is selected by the provider from approved typed
        # capabilities. The classifier remains only a fast path for legacy live
        # turns; source-backed non-casual turns also receive the planner so a
        # product or category reference is not lost before tool selection.
        is_live_operational = _is_live_operational_request(owner_message.content)
        has_active_operational_source = False
        if profiles is not None and not _is_general_conversation_request(
            owner_message.content
        ):
            probe_executor = OperationalToolExecutor(session, profiles, settings)
            has_active_operational_source = (
                probe_executor._active_source(business_id) is not None
            )
        if is_live_operational or (
            profiles is not None and has_active_operational_source
        ):
            executor = (
                OperationalToolExecutor(session, profiles, settings)
                if profiles is not None
                else None
            )
            available = (
                executor.available_definitions(user, business_id)
                if executor is not None
                else ()
            )
            if is_live_operational:
                matching_names = _matching_operational_tools(owner_message.content)
                available = tuple(
                    definition
                    for definition in available
                    if definition.name in matching_names
                )
            if not available:
                live_result = OwnerChatResult(
                    reply=_live_operational_reply(
                        owner_message.content, business.default_language
                    )
                )
                _persist_result(
                    session, business_id, claim, live_result, None, None, ()
                )
                return True
            provider_definitions = tuple(
                ProviderToolDefinition(**definition.provider_schema())
                for definition in available
            )
            category_candidates = tuple(
                ProviderCategoryCandidate(
                    external_category_id=candidate.external_category_id,
                    label=candidate.label,
                )
                for candidate in executor.category_candidates(user, business_id)
            )
            initial = _build_operational_request(
                session,
                business_id,
                claim.message_id,
                settings,
                provider_definitions,
                category_candidates=category_candidates,
            )
            generation_attempt = _admit_provider_generation(session, business_id, claim)
            try:
                reservation = reserve_owner_chat_usage(
                    session,
                    business=initial.business,
                    user=user,
                    owner_message_id=claim.message_id,
                    generation_attempt=generation_attempt,
                    estimated_input_tokens=(
                        provider.estimate_input_tokens(initial.request)
                        * MAX_OPERATIONAL_PROVIDER_CALLS
                    ),
                    max_output_tokens=(
                        initial.request.max_output_tokens
                        * MAX_OPERATIONAL_PROVIDER_CALLS
                    ),
                    lease_seconds=settings.owner_chat_generation_lease_seconds,
                )
            except Exception:
                session.rollback()
                _undo_pre_provider_admission(
                    session, business_id, claim, generation_attempt
                )
                raise
            if executor is None:  # pragma: no cover - guarded above
                raise RuntimeError("Operational executor is unavailable.")
            result, aggregate_usage = _run_operational_loop(
                session,
                business_id,
                claim,
                user,
                provider,
                settings,
                executor,
                provider_definitions,
                category_candidates,
            )
            _persist_result(
                session,
                business_id,
                claim,
                result,
                reservation,
                aggregate_usage,
                (),
            )
            return True
        if _requires_business_evidence(owner_message.content):
            prepared = _build_provider_request(
                session, user, business_id, claim.message_id, settings
            )
        else:
            prepared = _build_conversation_request(
                session, business_id, claim.message_id, settings
            )
        request = prepared.request
        business = prepared.business
        conflict_labels = _conflicting_source_labels(request.sources)
        if not prepared.has_usable_evidence:
            # A connected operational source is the authoritative fallback for
            # business questions that retrieval cannot answer. The provider still
            # performs typed intent selection; no message vocabulary is routed here.
            operational_executor = (
                OperationalToolExecutor(session, profiles, settings)
                if profiles is not None
                else None
            )
            operational_definitions = (
                operational_executor.available_definitions(user, business_id)
                if operational_executor is not None
                else ()
            )
            if operational_definitions:
                provider_definitions = tuple(
                    ProviderToolDefinition(**definition.provider_schema())
                    for definition in operational_definitions
                )
                category_candidates = tuple(
                    ProviderCategoryCandidate(
                        external_category_id=candidate.external_category_id,
                        label=candidate.label,
                    )
                    for candidate in operational_executor.category_candidates(
                        user, business_id
                    )
                )
                initial = _build_operational_request(
                    session,
                    business_id,
                    claim.message_id,
                    settings,
                    provider_definitions,
                    category_candidates=category_candidates,
                )
                generation_attempt = _admit_provider_generation(
                    session, business_id, claim
                )
                try:
                    reservation = reserve_owner_chat_usage(
                        session,
                        business=initial.business,
                        user=user,
                        owner_message_id=claim.message_id,
                        generation_attempt=generation_attempt,
                        estimated_input_tokens=(
                            provider.estimate_input_tokens(initial.request)
                            * MAX_OPERATIONAL_PROVIDER_CALLS
                        ),
                        max_output_tokens=(
                            initial.request.max_output_tokens
                            * MAX_OPERATIONAL_PROVIDER_CALLS
                        ),
                        lease_seconds=settings.owner_chat_generation_lease_seconds,
                    )
                except Exception:
                    session.rollback()
                    _undo_pre_provider_admission(
                        session, business_id, claim, generation_attempt
                    )
                    raise
                result, aggregate_usage = _run_operational_loop(
                    session,
                    business_id,
                    claim,
                    user,
                    provider,
                    settings,
                    operational_executor,
                    provider_definitions,
                    category_candidates,
                )
                _persist_result(
                    session,
                    business_id,
                    claim,
                    result,
                    reservation,
                    aggregate_usage,
                    (),
                )
                return True
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
                request.sources,
            )
            return False
        estimated_input_tokens = provider.estimate_input_tokens(request)
        generation_attempt = _admit_provider_generation(session, business_id, claim)
        try:
            reservation = reserve_owner_chat_usage(
                session,
                business=business,
                user=user,
                owner_message_id=claim.message_id,
                generation_attempt=generation_attempt,
                estimated_input_tokens=estimated_input_tokens,
                max_output_tokens=request.max_output_tokens,
                lease_seconds=settings.owner_chat_generation_lease_seconds,
            )
        except Exception:
            session.rollback()
            _undo_pre_provider_admission(
                session, business_id, claim, generation_attempt
            )
            raise
        result = _validate_result(provider.generate(request), request)
        if request.mode == "conversation":
            result = OwnerChatResult(
                reply=(
                    _missing_knowledge_reply(
                        request.messages[-1].content, business.default_language
                    )
                    if result.requires_business_knowledge
                    else result.reply
                ),
                usage=result.usage,
                provider_identifier=result.provider_identifier,
                model_identifier=result.model_identifier,
            )
        if conflict_labels:
            result = _enforce_conflict_result(
                result,
                request.messages[-1].content,
                business.default_language,
                request.sources,
                conflict_labels,
            )
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
        _logger.error(
            "owner-chat provider error: %s reason=%s usage_uncertain=%s",
            type(exc).__name__,
            exc.reason,
            exc.usage_uncertain,
        )
        # rate_limited is an explicit HTTP rejection — the provider never processed
        # the request so no tokens were consumed; release the reservation instead of
        # charging the worst-case estimate with outcome="uncertain".
        rate_limited = (
            isinstance(exc, OwnerChatProviderUnavailable)
            and exc.reason == "rate_limited"
        )
        outcome = (
            "reported_failure"
            if exc.usage is not None
            else "release"
            if not exc.usage_uncertain or rate_limited
            else "uncertain"
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
        raise _safe_provider_failure(exc) from None
    except ApplicationError:
        session.rollback()
        _mark_failed(
            session,
            claim,
            reservation=reservation,
            outcome="release" if reservation is not None else None,
        )
        raise
    except ToolExecutionError:
        session.rollback()
        _mark_failed(
            session,
            claim,
            reservation=reservation,
            usage=aggregate_usage,
            outcome=(
                "reported_failure"
                if reservation is not None and aggregate_usage is not None
                else "release"
                if reservation is not None
                else None
            ),
        )
        raise _provider_unavailable() from None
    except Exception:
        _logger.error(
            "unexpected exception in _generate_claimed_turn:\n%s",
            traceback.format_exc(),
        )
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
            session, business_id, claim, result, reservation, usage, request.sources
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
    return False


def submit_owner_message(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    body: OwnerMessageRequest,
    provider: OwnerChatProvider,
    settings: Settings,
    profiles: ConnectionProfileRegistry | None = None,
    *,
    conversation_id: uuid.UUID | None = None,
) -> OwnerTurnResponse:
    """Persist and process only the idempotent owner turn from this request."""
    _eligible_business(session, user, business_id)
    if conversation_id is None:
        conversation = get_default_conversation(session, user, business_id, create=True)
        if conversation is None:  # pragma: no cover - create=True
            raise _provider_unavailable()
    else:
        conversation = load_conversation(session, user, business_id, conversation_id)
    owner_message, replayed, claim = _create_or_reuse_owner_message(
        session, conversation.id, body, settings
    )
    completed = _completed_turn(session, owner_message, replayed)
    if completed is not None:
        return completed
    if owner_message.generation_state == ChatGenerationState.FAILED:
        raise _owner_turn_failed()

    if claim is not None:
        operational_turn = _generate_claimed_turn(
            session, business_id, claim, user, provider, settings, profiles
        )
        session.expire_all()
        refreshed = session.get(OwnerChatMessage, owner_message.id)
        if refreshed is not None:
            completed = _completed_turn(session, refreshed, replayed)
            if completed is not None:
                if not operational_turn:
                    _enqueue_summary_safely(conversation.id, settings)
                return completed
            if refreshed.generation_state == ChatGenerationState.FAILED:
                raise _owner_turn_failed()
        raise _conversation_busy()

    deadline = time.monotonic() + settings.owner_chat_generation_wait_seconds
    while time.monotonic() < deadline:
        session.expire_all()
        refreshed = session.get(OwnerChatMessage, owner_message.id)
        if refreshed is not None:
            completed = _completed_turn(session, refreshed, replayed)
            if completed is not None:
                return completed
            if refreshed.generation_state == ChatGenerationState.FAILED:
                raise _owner_turn_failed()
        session.rollback()
        time.sleep(0.025)
    raise _conversation_busy()


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
    *,
    conversation_id: uuid.UUID | None = None,
) -> ConversationHistoryResponse:
    load_full_access_business(session, user, business_id)
    conversation = (
        load_conversation(session, user, business_id, conversation_id)
        if conversation_id is not None
        else get_default_conversation(session, user, business_id, create=False)
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


def _enqueue_summary_safely(conversation_id: uuid.UUID, settings: Settings) -> None:
    try:
        from app.worker.conversation_summary import enqueue_conversation_summary

        enqueue_conversation_summary(conversation_id, settings)
    except Exception:
        # Summary memory is asynchronous and must never fail an owner response.
        return
