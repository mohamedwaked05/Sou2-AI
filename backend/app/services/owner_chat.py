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
from dataclasses import dataclass, replace
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
    ProviderLocationCandidate,
    ProviderMessage,
    ProviderProductCandidate,
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
    UserOperationalPreference,
)
from app.integrations.profiles import ConnectionProfileRegistry
from app.rag.embeddings import create_embedding_provider
from app.rag.retrieval import retrieve
from app.schemas.operational import (
    InventoryResult,
    MetricCapabilityResult,
    RestockingRecommendationsResult,
)
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
    CURRENT_INVENTORY_TOOL,
    OperationalToolExecutor,
    ToolExecutionError,
)

_logger = logging.getLogger(__name__)

CHAT_CONTEXT_MESSAGE_LIMIT = 12
# A category inventory turn may need a planner, bounded semantic resolver, and
# response-only synthesis. Reserve for the complete bounded path up front.
MAX_OPERATIONAL_PROVIDER_CALLS = 3
CATEGORY_RESOLUTION_MAX_OUTPUT_TOKENS = 64
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

_SAFE_FINANCIAL_METRICS = frozenset(
    {"revenue", "gross_profit", "net_profit", "sales_count", "inventory_value"}
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
    location_candidates: tuple[ProviderLocationCandidate, ...] = (),
    pending_product_candidates: tuple[ProviderProductCandidate, ...] = (),
) -> _PreparedTurn:
    owner_message = session.get(OwnerChatMessage, owner_message_id)
    business = _load_provider_business(session, business_id)
    if owner_message is None or business is None:
        raise _provider_unavailable()
    # Operational filters must describe the current turn. Persisted preferences and
    # pending clarification state are resolved by the backend, not inferred from
    # unrelated conversation history.
    messages = (
        ProviderMessage(role=str(owner_message.role), content=owner_message.content),
    )
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
        location_candidates=location_candidates,
        pending_product_candidates=pending_product_candidates,
    )
    session.commit()
    return _PreparedTurn(request=request, business=business, has_usable_evidence=True)


def _build_operational_synthesis_request(
    session: Session,
    business_id: uuid.UUID,
    owner_message_id: uuid.UUID,
    settings: Settings,
    result: ProviderToolResult,
    *,
    requested_at: datetime,
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
        requested_at=requested_at,
        max_output_tokens=settings.owner_chat_max_output_tokens,
        mode="operational_synthesis",
        tools=(),
        tool_results=(result,),
        validated_result_status=_operational_synthesis_status(result.output),
        category_candidates=(),
    )
    session.commit()
    return _PreparedTurn(request=request, business=business, has_usable_evidence=True)


def _build_category_resolution_request(
    request: OwnerChatRequest, category_query: str
) -> OwnerChatRequest:
    """Create an isolated, compact request for bounded category matching."""

    return replace(
        request,
        messages=(ProviderMessage(role="owner", content=category_query),),
        rolling_summary=None,
        max_output_tokens=min(
            request.max_output_tokens, CATEGORY_RESOLUTION_MAX_OUTPUT_TOKENS
        ),
        mode="category_resolution",
        tools=(),
        tool_results=(),
        location_candidates=(),
        pending_product_candidates=(),
    )


def _operational_synthesis_status(output: object) -> str:
    """Classify backend-validated tool output for response-only verification."""

    if isinstance(output, MetricCapabilityResult):
        return "unsupported" if output.status == "unsupported" else "data"
    if isinstance(output, InventoryResult):
        if output.resolution is not None and output.resolution.status != "resolved":
            return output.resolution.status
        if (
            output.category_resolution is not None
            and output.category_resolution.status != "resolved"
        ):
            return output.category_resolution.status
        return "data" if output.items else "empty"
    if isinstance(output, RestockingRecommendationsResult):
        if output.resolution is not None and output.resolution.status != "resolved":
            return output.resolution.status
        if (
            output.category_resolution is not None
            and output.category_resolution.status != "resolved"
        ):
            return output.category_resolution.status
        return "data" if output.items else "empty"
    if not isinstance(output, dict):
        return "other"
    if output.get("capability") == "inventory_location_preference":
        return "preference"
    status = output.get("status")
    if status == "unsupported":
        return "unsupported"
    if status in {"ambiguous", "not_found"}:
        return status
    resolution = output.get("resolution")
    if isinstance(resolution, dict):
        resolution_status = resolution.get("status")
        if resolution_status in {"ambiguous", "not_found"}:
            return resolution_status
    category_resolution = output.get("category_resolution")
    if isinstance(category_resolution, dict):
        category_status = category_resolution.get("status")
        if category_status in {"ambiguous", "not_found"}:
            return category_status
    items = output.get("items")
    if isinstance(items, list):
        return "data" if items else "empty"
    return "other"


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
    if request.mode == "category_resolution":
        output = json.dumps(
            {
                "status": result.category_resolution_status,
                "candidate_references": result.category_candidate_references,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    else:
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


def _planned_metric(arguments: object) -> str | None:
    if not isinstance(arguments, dict):
        return None
    metric = arguments.get("metric")
    return (
        metric
        if isinstance(metric, str) and metric in _SAFE_FINANCIAL_METRICS
        else None
    )


def _consistent_operational_plan(
    result: OwnerChatResult,
    category_candidates: tuple[ProviderCategoryCandidate, ...],
) -> tuple[OwnerChatResult, str]:
    """Derive inventory execution from validated semantic intent, not tool choice."""

    if result.semantic_operation not in {
        "inventory_product",
        "inventory_category",
        "inventory_list",
    }:
        return result, "accepted"

    arguments: dict[str, object] = {}
    if result.semantic_operation == "inventory_product":
        arguments["product_filter"] = result.entity_query
    elif result.semantic_operation == "inventory_category":
        candidate = next(
            (
                item
                for item in category_candidates
                if item.external_category_id == result.category_candidate_reference
            ),
            None,
        )
        if result.category_candidate_reference is not None and candidate is None:
            raise OwnerChatProviderInvalidResponse(
                reason="invalid_category_candidate_reference"
            )
        arguments["category_filter"] = (
            candidate.label if candidate is not None else result.entity_query
        )
    if (
        result.tool_name == CURRENT_INVENTORY_TOOL
        and isinstance(result.tool_arguments, dict)
        and isinstance(result.tool_arguments.get("location_reference"), str)
    ):
        arguments["location_reference"] = result.tool_arguments["location_reference"]

    is_consistent = (
        result.decision == "tool"
        and result.tool_name == CURRENT_INVENTORY_TOOL
        and result.tool_arguments == arguments
    )
    if is_consistent:
        return result, "accepted"
    return (
        replace(
            result,
            decision="tool",
            reply="",
            tool_name=CURRENT_INVENTORY_TOOL,
            tool_arguments=arguments,
            preference_key=None,
            location_reference=None,
        ),
        "normalized",
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


def _operational_synthesis_fallback(
    output: object,
    usage: TokenUsage,
    provider_identifier: str | None,
    model_identifier: str | None,
) -> OwnerChatResult:
    if isinstance(output, InventoryResult):
        if (
            output.category_resolution is not None
            and output.category_resolution.status == "not_found"
        ):
            reply = "The requested category was not found in the current catalogue."
        elif output.items:
            entries = []
            for item in output.items[:10]:
                location = item.branch_name or item.warehouse_name
                entries.append(
                    f"{item.product.name}: {item.available_quantity} available "
                    f"at {location}"
                )
            reply = "Current inventory: " + "; ".join(entries) + "."
        else:
            reply = "No matching stock is currently available."
    elif isinstance(output, MetricCapabilityResult) and output.status == "unsupported":
        missing = ", ".join(
            "cost/COGS" if item == "cost_cogs" else item.replace("_", " ")
            for item in output.missing_inputs
        )
        alternatives = ", ".join(
            item.replace("_", " ") for item in output.supported_metrics
        )
        reply = (
            f"Accurate {output.requested_metric.replace('_', ' ')} cannot be "
            f"calculated because {missing} is not connected. "
            f"Available alternatives: {alternatives}."
        )
    elif isinstance(output, dict) and output.get("capability") == (
        "inventory_location_preference"
    ):
        action = output.get("action")
        location = output.get("location")
        if (
            action == "saved"
            and isinstance(location, dict)
            and isinstance(location.get("label"), str)
        ):
            reply = (
                f"I will use {location['label']} by default for inventory questions "
                "unless you specify another location."
            )
        elif action == "cleared":
            reply = "I will no longer use a default location for inventory questions."
        else:
            reply = "I could not save that inventory location preference."
    elif (
        isinstance(output, dict)
        and output.get("capability") == "operational_planning"
        and output.get("source_connected") is True
    ):
        reply = (
            "The live operational source is available, but I could not determine "
            "a safe lookup to run. Please clarify your request."
        )
    else:
        reply = (
            "The requested operational lookup completed, but I could not safely "
            "format its validated result."
        )
    return OwnerChatResult(
        reply=reply,
        usage=usage,
        provider_identifier=provider_identifier,
        model_identifier=model_identifier,
        decision="final",
    )


@dataclass(frozen=True)
class _InventoryLocationPreparation:
    arguments: dict[str, object]
    result: ProviderToolResult | None
    location_source: str
    location_input_kind: str
    preference_loaded: bool
    preference_applied: bool
    location_resolution: str


def _location_resolution_outcome(status: str) -> str:
    return {
        "resolved": "one",
        "ambiguous": "multiple",
        "not_found": "zero",
    }.get(status, "zero")


def _prepare_inventory_location_arguments(
    session: Session,
    executor: OperationalToolExecutor,
    user: User,
    business_id: uuid.UUID,
    arguments: dict[str, object],
) -> _InventoryLocationPreparation:
    updated = dict(arguments)
    location_reference = updated.pop("location_reference", None)
    branch_reference = updated.pop("branch_external_id", None)
    warehouse_reference = updated.pop("warehouse_external_id", None)
    if (
        sum(
            bool(reference)
            for reference in (location_reference, branch_reference, warehouse_reference)
        )
        > 1
    ):
        raise ToolExecutionError("invalid_arguments")
    current_turn_reference = (
        location_reference or branch_reference or warehouse_reference
    )
    if current_turn_reference:
        if not isinstance(current_turn_reference, str):
            raise ToolExecutionError("invalid_arguments")
        resolution = executor.resolve_location(
            user, business_id, current_turn_reference
        )
        outcome = _location_resolution_outcome(resolution.status)
        if resolution.status != "resolved" or resolution.location is None:
            return _InventoryLocationPreparation(
                arguments=updated,
                result=ProviderToolResult(
                    tool_name="inventory_location",
                    output={
                        "capability": "inventory_location",
                        "status": resolution.status,
                        "candidates": [
                            {
                                "label": candidate.label,
                                "location_type": candidate.location_type,
                            }
                            for candidate in resolution.candidates
                        ],
                    },
                ),
                location_source="current_turn",
                location_input_kind="label",
                preference_loaded=False,
                preference_applied=False,
                location_resolution=outcome,
            )
        location = resolution.location
        updated[
            "branch_external_id"
            if location.location_type == "branch"
            else "warehouse_external_id"
        ] = location.external_location_id
        return _InventoryLocationPreparation(
            arguments=updated,
            result=None,
            location_source="current_turn",
            location_input_kind="label",
            preference_loaded=False,
            preference_applied=False,
            location_resolution=outcome,
        )
    source = executor._active_source(business_id)
    if source is None:
        _logger.info(
            "owner_chat_inventory_preference preference_lookup=source_mismatch"
        )
        return _InventoryLocationPreparation(
            arguments=updated,
            result=None,
            location_source="none",
            location_input_kind="none",
            preference_loaded=False,
            preference_applied=False,
            location_resolution="zero",
        )
    preference = session.scalar(
        select(UserOperationalPreference).where(
            UserOperationalPreference.user_id == user.id,
            UserOperationalPreference.business_id == business_id,
            UserOperationalPreference.source_id == source.id,
            UserOperationalPreference.preference_key == "default_inventory_location",
        )
    )
    if preference is None:
        _logger.info("owner_chat_inventory_preference preference_lookup=not_found")
        return _InventoryLocationPreparation(
            arguments=updated,
            result=None,
            location_source="none",
            location_input_kind="none",
            preference_loaded=False,
            preference_applied=False,
            location_resolution="zero",
        )
    try:
        resolution = executor.resolve_location(
            user, business_id, preference.location_external_id
        )
    except ToolExecutionError:
        _logger.info(
            "owner_chat_inventory_preference preference_lookup=invalid_reference"
        )
        return _InventoryLocationPreparation(
            arguments=updated,
            result=None,
            location_source="none",
            location_input_kind="none",
            preference_loaded=True,
            preference_applied=False,
            location_resolution="zero",
        )
    if (
        resolution.status != "resolved"
        or resolution.location is None
        or resolution.location.external_location_id != preference.location_external_id
        or resolution.location.location_type != preference.location_type
    ):
        _logger.info(
            "owner_chat_inventory_preference preference_lookup=invalid_reference"
        )
        session.delete(preference)
        session.commit()
        return _InventoryLocationPreparation(
            arguments=updated,
            result=ProviderToolResult(
                tool_name="inventory_location_preference",
                output={
                    "action": "invalidated",
                    "capability": "inventory_location_preference",
                },
            ),
            location_source="none",
            location_input_kind="none",
            preference_loaded=True,
            preference_applied=False,
            location_resolution=_location_resolution_outcome(resolution.status),
        )
    updated[
        "branch_external_id"
        if preference.location_type == "branch"
        else "warehouse_external_id"
    ] = preference.location_external_id
    _logger.info("owner_chat_inventory_preference preference_lookup=found")
    return _InventoryLocationPreparation(
        arguments=updated,
        result=None,
        location_source="saved",
        location_input_kind="validated_reference",
        preference_loaded=True,
        preference_applied=True,
        location_resolution="one",
    )


def _save_inventory_location_preference(
    session: Session,
    executor: OperationalToolExecutor,
    user: User,
    business_id: uuid.UUID,
    action: str,
    location_reference: str | None,
) -> ProviderToolResult:
    source = executor._active_source(business_id)
    if source is None:
        raise ToolExecutionError("integration_unavailable")
    statement = select(UserOperationalPreference).where(
        UserOperationalPreference.user_id == user.id,
        UserOperationalPreference.business_id == business_id,
        UserOperationalPreference.source_id == source.id,
        UserOperationalPreference.preference_key == "default_inventory_location",
    )
    existing = session.scalar(statement)
    if action == "clear_preference":
        if existing is not None:
            session.delete(existing)
            session.commit()
        return ProviderToolResult(
            tool_name="inventory_location_preference",
            output={"action": "cleared", "capability": "inventory_location_preference"},
        )
    if location_reference is None:
        raise ToolExecutionError("invalid_arguments")
    resolution = executor.resolve_location(user, business_id, location_reference)
    if resolution.status != "resolved" or resolution.location is None:
        return ProviderToolResult(
            tool_name="inventory_location_preference",
            output={
                "action": "not_saved",
                "capability": "inventory_location_preference",
                "resolution": {
                    "status": resolution.status,
                    "candidates": [
                        {
                            "label": candidate.label,
                            "location_type": candidate.location_type,
                        }
                        for candidate in resolution.candidates
                    ],
                },
            },
        )
    location = resolution.location
    if existing is None:
        session.add(
            UserOperationalPreference(
                user_id=user.id,
                business_id=business_id,
                source_id=source.id,
                preference_key="default_inventory_location",
                location_type=location.location_type,
                location_external_id=location.external_location_id,
            )
        )
    else:
        existing.location_type = location.location_type
        existing.location_external_id = location.external_location_id
    session.commit()
    return ProviderToolResult(
        tool_name="inventory_location_preference",
        output={
            "action": "saved",
            "capability": "inventory_location_preference",
            "location": {
                "label": location.label,
                "location_type": location.location_type,
            },
        },
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


def _bounded_category_resolution_reply(
    candidate_references: tuple[str, ...],
    category_candidates: tuple[ProviderCategoryCandidate, ...],
    message: str,
    default_language: str,
) -> str | None:
    labels = tuple(
        candidate.label
        for candidate in category_candidates
        if candidate.external_category_id in candidate_references
    )
    if len(labels) < 2:
        return None
    candidate_text = "; ".join(labels[:5])
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


def _exact_category_candidate_reference(
    category_query: str,
    category_candidates: tuple[ProviderCategoryCandidate, ...],
) -> str | None:
    normalized_query = category_query.strip().casefold()
    matches = tuple(
        candidate.external_category_id
        for candidate in category_candidates
        if candidate.label.strip().casefold() == normalized_query
    )
    return matches[0] if len(matches) == 1 else None


def _pending_product_candidates(
    session: Session,
    executor: OperationalToolExecutor,
    user: User,
    business_id: uuid.UUID,
    owner_message_id: uuid.UUID,
) -> tuple[ProviderProductCandidate, ...]:
    """Reconstruct one source-backed product clarification without chat history."""

    owner_message = session.get(OwnerChatMessage, owner_message_id)
    if owner_message is None or owner_message.sequence_number < 3:
        return ()
    previous_owner = session.scalar(
        select(OwnerChatMessage).where(
            OwnerChatMessage.conversation_id == owner_message.conversation_id,
            OwnerChatMessage.sequence_number == owner_message.sequence_number - 2,
            OwnerChatMessage.role == ChatMessageRole.OWNER,
            OwnerChatMessage.generation_state == ChatGenerationState.COMPLETED,
        )
    )
    previous_assistant = session.scalar(
        select(OwnerChatMessage).where(
            OwnerChatMessage.conversation_id == owner_message.conversation_id,
            OwnerChatMessage.sequence_number == owner_message.sequence_number - 1,
            OwnerChatMessage.role == ChatMessageRole.ASSISTANT,
            OwnerChatMessage.reply_to_message_id == previous_owner.id
            if previous_owner is not None
            else False,
        )
    )
    if previous_owner is None or previous_assistant is None:
        return ()
    try:
        resolution = executor.resolve_product(user, business_id, previous_owner.content)
    except ToolExecutionError:
        return ()
    if resolution.status != "ambiguous":
        return ()
    candidates = tuple(
        ProviderProductCandidate(label=candidate.name, sku=candidate.sku)
        for candidate in resolution.candidates
    )
    if not candidates or not all(
        candidate.label in previous_assistant.content for candidate in candidates
    ):
        return ()
    return candidates


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
    location_candidates: tuple[ProviderLocationCandidate, ...] = (),
) -> tuple[OwnerChatResult, TokenUsage]:
    tool_results: list[ProviderToolResult] = []
    aggregate_usage: TokenUsage | None = None
    requested_at = utc_now()
    pending_product_candidates = _pending_product_candidates(
        session, executor, user, business_id, claim.message_id
    )

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
            location_candidates=location_candidates,
            pending_product_candidates=pending_product_candidates,
        )
        request = prepared.request
        try:
            result = _validate_result(provider.generate(request), request)
        except OwnerChatProviderError as exc:
            _logger.info(
                "owner_chat_operational_plan validation=rejected reason=%s",
                exc.reason,
            )
            if aggregate_usage is not None and not exc.usage_uncertain:
                exc.usage = (
                    _add_usage(aggregate_usage, exc.usage)
                    if exc.usage is not None
                    else aggregate_usage
                )
            raise
        original_action = result.decision
        # Only the dedicated resolver may supply an executable category reference.
        # A planner-provided reference is untrusted until it is revalidated against
        # the exact bounded candidates in a separate compact request.
        result = replace(result, category_candidate_reference=None)
        result, consistency_outcome = _consistent_operational_plan(
            result, request.category_candidates
        )
        _logger.info(
            "owner_chat_operational_plan semantic_operation=%s entity_kind=%s "
            "original_action=%s effective_action=%s consistency_outcome=%s "
            "effective_tool=%s metric=%s validation=accepted",
            result.semantic_operation,
            result.entity_kind,
            original_action,
            result.decision,
            consistency_outcome,
            result.tool_name if result.decision == "tool" else None,
            _planned_metric(result.tool_arguments),
        )
        call_usage = _usage_for_result(provider, request, result)
        if call_usage.output_tokens > request.max_output_tokens:
            raise OwnerChatProviderInvalidResponse(
                usage=_add_usage(aggregate_usage, call_usage),
                provider_identifier=result.provider_identifier,
                model_identifier=result.model_identifier,
            )
        aggregate_usage = _add_usage(aggregate_usage, call_usage)

        if (
            result.semantic_operation == "inventory_category"
            and result.entity_query is not None
            and request.category_candidates
        ):
            exact_reference = _exact_category_candidate_reference(
                result.entity_query, request.category_candidates
            )
            if exact_reference is not None:
                result = replace(result, category_candidate_reference=exact_reference)
                result, redispatch_outcome = _consistent_operational_plan(
                    result, request.category_candidates
                )
                _logger.info(
                    "owner_chat_category_resolution outcome=exact redispatch=%s",
                    redispatch_outcome,
                )
            else:
                category_request = _build_category_resolution_request(
                    request, result.entity_query
                )
                try:
                    category_result = _validate_result(
                        provider.generate(category_request), category_request
                    )
                except OwnerChatProviderError as exc:
                    failure_usage = exc.usage
                    if failure_usage is None and exc.usage_uncertain:
                        estimated_input = provider.estimate_input_tokens(
                            category_request
                        )
                        failure_usage = TokenUsage(
                            input_tokens=estimated_input,
                            output_tokens=category_request.max_output_tokens,
                            total_tokens=(
                                estimated_input + category_request.max_output_tokens
                            ),
                            authoritative=False,
                        )
                    if failure_usage is not None:
                        aggregate_usage = _add_usage(aggregate_usage, failure_usage)
                    _logger.info(
                        "owner_chat_category_resolution outcome=provider_failure "
                        "failure_reason=%s fallback=source_lookup",
                        exc.reason or "unknown",
                    )
                else:
                    category_usage = _usage_for_result(
                        provider, category_request, category_result
                    )
                    if (
                        category_usage.output_tokens
                        > category_request.max_output_tokens
                    ):
                        aggregate_usage = _add_usage(aggregate_usage, category_usage)
                        _logger.info(
                            "owner_chat_category_resolution outcome=invalid "
                            "failure_reason=output_token_limit fallback=source_lookup"
                        )
                    else:
                        aggregate_usage = _add_usage(aggregate_usage, category_usage)
                        _logger.info(
                            "owner_chat_category_resolution outcome=%s "
                            "candidate_count=%s",
                            category_result.category_resolution_status,
                            len(category_result.category_candidate_references),
                        )
                        if category_result.category_resolution_status == "matched":
                            result = replace(
                                result,
                                category_candidate_reference=(
                                    category_result.category_candidate_references[0]
                                ),
                            )
                            result, redispatch_outcome = _consistent_operational_plan(
                                result, request.category_candidates
                            )
                            _logger.info(
                                "owner_chat_category_resolution outcome=matched "
                                "redispatch=%s",
                                redispatch_outcome,
                            )
                        elif category_result.category_resolution_status == "ambiguous":
                            reply = _bounded_category_resolution_reply(
                                category_result.category_candidate_references,
                                request.category_candidates,
                                request.messages[-1].content,
                                prepared.business.default_language,
                            )
                            if reply is None:
                                raise OwnerChatProviderInvalidResponse(
                                    reason="invalid_category_resolution_references"
                                )
                            return (
                                OwnerChatResult(
                                    reply=reply,
                                    usage=aggregate_usage,
                                    provider_identifier=(
                                        category_result.provider_identifier
                                    ),
                                    model_identifier=category_result.model_identifier,
                                ),
                                aggregate_usage,
                            )

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
            _logger.info(
                "owner_chat_operational_dispatch outcome=provider_unavailable "
                "fallback_reason=provider_unavailable final_synthesis_path=provider"
            )
            return _operational_result_with_usage(
                result, aggregate_usage
            ), aggregate_usage
        if result.decision == "final":
            if not tool_results:
                _logger.info(
                    "owner_chat_operational_dispatch outcome=final_without_tool "
                    "fallback_reason=provider_final_without_tool_active_source "
                    "final_synthesis_path=deterministic"
                )
                return (
                    _operational_synthesis_fallback(
                        {
                            "capability": "operational_planning",
                            "source_connected": True,
                            "status": "invalid_final_without_tool",
                        },
                        aggregate_usage,
                        result.provider_identifier,
                        result.model_identifier,
                    ),
                    aggregate_usage,
                )
            return _operational_result_with_usage(
                result, aggregate_usage
            ), aggregate_usage

        if result.decision in {"set_preference", "clear_preference"}:
            try:
                preference_result = _save_inventory_location_preference(
                    session,
                    executor,
                    user,
                    business_id,
                    result.decision,
                    result.location_reference,
                )
            except ToolExecutionError:
                _logger.info(
                    "owner_chat_operational_dispatch outcome=preference_error "
                    "final_synthesis_path=deterministic"
                )
                return (
                    _operational_synthesis_fallback(
                        {
                            "action": "not_saved",
                            "capability": "inventory_location_preference",
                        },
                        aggregate_usage,
                        result.provider_identifier,
                        result.model_identifier,
                    ),
                    aggregate_usage,
                )
            synthesis_prepared = _build_operational_synthesis_request(
                session,
                business_id,
                claim.message_id,
                settings,
                preference_result,
                requested_at=requested_at,
            )
            synthesis_request = synthesis_prepared.request
            try:
                synthesis = _validate_result(
                    provider.generate(synthesis_request), synthesis_request
                )
                synthesis_usage = _usage_for_result(
                    provider, synthesis_request, synthesis
                )
                aggregate_usage = _add_usage(aggregate_usage, synthesis_usage)
                _logger.info(
                    "owner_chat_operational_synthesis outcome=preference "
                    "schema=response_only"
                )
                return (
                    _operational_result_with_usage(synthesis, aggregate_usage),
                    aggregate_usage,
                )
            except OwnerChatProviderError:
                _logger.info(
                    "owner_chat_operational_synthesis outcome=preference_failure "
                    "schema=response_only"
                )
                return (
                    _operational_synthesis_fallback(
                        preference_result.output,
                        aggregate_usage,
                        result.provider_identifier,
                        result.model_identifier,
                    ),
                    aggregate_usage,
                )

        arguments = result.tool_arguments or {}
        location_source = "none"
        location_input_kind = "none"
        preference_loaded = False
        preference_applied = False
        location_resolution = "zero"
        if result.tool_name == "current_inventory":
            product_filter = arguments.get("product_filter")
            category_filter = arguments.get("category_filter")
            product_input_kind = (
                "query"
                if isinstance(product_filter, str) and product_filter
                else "none"
            )
            category_input_kind = (
                "query"
                if isinstance(category_filter, str) and category_filter
                else "none"
            )
            product_query_token_count = (
                len(re.findall(r"[^\W_]+", product_filter, flags=re.UNICODE))
                if product_input_kind == "query"
                else 0
            )
            category_query_token_count = (
                len(re.findall(r"[^\W_]+", category_filter, flags=re.UNICODE))
                if category_input_kind == "query"
                else 0
            )
            _logger.info(
                "owner_chat_inventory_plan product_input_kind=%s "
                "product_query_token_count=%s category_input_kind=%s "
                "category_query_token_count=%s",
                product_input_kind,
                product_query_token_count,
                category_input_kind,
                category_query_token_count,
            )
            try:
                location_preparation = _prepare_inventory_location_arguments(
                    session, executor, user, business_id, arguments
                )
            except ToolExecutionError:
                _logger.info(
                    "owner_chat_inventory_location outcome=error "
                    "location_source=current_turn location_input_kind=label"
                )
                return (
                    OwnerChatResult(
                        reply=_live_operational_reply(
                            request.messages[-1].content,
                            prepared.business.default_language,
                        ),
                        usage=aggregate_usage,
                        provider_identifier=result.provider_identifier,
                        model_identifier=result.model_identifier,
                        decision="unavailable",
                    ),
                    aggregate_usage,
                )
            arguments = location_preparation.arguments
            location_source = location_preparation.location_source
            location_input_kind = location_preparation.location_input_kind
            preference_loaded = location_preparation.preference_loaded
            preference_applied = location_preparation.preference_applied
            location_resolution = location_preparation.location_resolution
            if location_preparation.result is not None:
                synthesis_prepared = _build_operational_synthesis_request(
                    session,
                    business_id,
                    claim.message_id,
                    settings,
                    location_preparation.result,
                    requested_at=requested_at,
                )
                synthesis_request = synthesis_prepared.request
                try:
                    synthesis = _validate_result(
                        provider.generate(synthesis_request), synthesis_request
                    )
                    synthesis_usage = _usage_for_result(
                        provider, synthesis_request, synthesis
                    )
                    aggregate_usage = _add_usage(aggregate_usage, synthesis_usage)
                    _logger.info(
                        "owner_chat_operational_synthesis "
                        "outcome=location_resolution schema=response_only"
                    )
                    return (
                        _operational_result_with_usage(synthesis, aggregate_usage),
                        aggregate_usage,
                    )
                except OwnerChatProviderError:
                    return (
                        _operational_synthesis_fallback(
                            location_preparation.result.output,
                            aggregate_usage,
                            result.provider_identifier,
                            result.model_identifier,
                        ),
                        aggregate_usage,
                    )
        try:
            executed = executor.execute(
                user=user,
                business_id=business_id,
                tool_name=result.tool_name,
                arguments=arguments,
            )
        except ToolExecutionError:
            _logger.info(
                "owner_chat_operational_dispatch outcome=tool_error "
                "fallback_reason=tool_execution_error "
                "final_synthesis_path=deterministic"
            )
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
        capability = getattr(executed.output, "capability", None)
        capability_status = getattr(executed.output, "status", None)
        _logger.info(
            "owner_chat_operational_dispatch outcome=executed capability=%s "
            "capability_outcome=%s final_synthesis_path=provider",
            capability,
            capability_status,
        )
        if isinstance(executed.output, InventoryResult):
            resolution_status = (
                executed.output.resolution.status
                if executed.output.resolution is not None
                else "none"
            )
            inventory_status = _operational_synthesis_status(executed.output)
            category_resolution_status = (
                executed.output.category_resolution.status
                if executed.output.category_resolution is not None
                else "none"
            )
            _logger.info(
                "owner_chat_inventory_result location_source=%s "
                "location_input_kind=%s preference_loaded=%s "
                "preference_applied=%s location_resolution=%s "
                "product_resolution=%s category_resolution=%s "
                "normalized_rows=%s tool_result=%s",
                location_source,
                location_input_kind,
                preference_loaded,
                preference_applied,
                location_resolution,
                _location_resolution_outcome(resolution_status)
                if resolution_status != "none"
                else "zero",
                _location_resolution_outcome(category_resolution_status)
                if category_resolution_status != "none"
                else "none",
                executed.output.metadata.row_count,
                inventory_status,
            )
        tool_result = ProviderToolResult(
            tool_name=executed.tool_name,
            output=executed.output.model_dump(mode="json"),
        )
        resolution_reply = _product_resolution_reply(
            [tool_result],
            request.messages[-1].content,
            prepared.business.default_language,
        )
        if resolution_reply is None:
            resolution_reply = _category_resolution_reply(
                [tool_result],
                request.messages[-1].content,
                prepared.business.default_language,
            )
        if resolution_reply is not None:
            return (
                OwnerChatResult(
                    reply=resolution_reply,
                    usage=aggregate_usage,
                    provider_identifier=result.provider_identifier,
                    model_identifier=result.model_identifier,
                    decision="final",
                ),
                aggregate_usage,
            )

        synthesis_prepared = _build_operational_synthesis_request(
            session,
            business_id,
            claim.message_id,
            settings,
            tool_result,
            requested_at=requested_at,
        )
        synthesis_request = synthesis_prepared.request
        try:
            synthesis = _validate_result(
                provider.generate(synthesis_request), synthesis_request
            )
        except OwnerChatProviderError as exc:
            failure_usage = exc.usage
            if failure_usage is None and exc.usage_uncertain:
                estimated_input = provider.estimate_input_tokens(synthesis_request)
                failure_usage = TokenUsage(
                    input_tokens=estimated_input,
                    output_tokens=synthesis_request.max_output_tokens,
                    total_tokens=estimated_input + synthesis_request.max_output_tokens,
                    authoritative=False,
                )
            if failure_usage is not None:
                aggregate_usage = _add_usage(aggregate_usage, failure_usage)
            assert aggregate_usage is not None
            _logger.info(
                "owner_chat_operational_synthesis outcome=provider_failure "
                "failure_reason=%s schema=response_only "
                "fallback_reason=provider_failure",
                exc.reason or "unknown",
            )
            return (
                _operational_synthesis_fallback(
                    executed.output,
                    aggregate_usage,
                    result.provider_identifier,
                    result.model_identifier,
                ),
                aggregate_usage,
            )
        synthesis_usage = _usage_for_result(provider, synthesis_request, synthesis)
        assert aggregate_usage is not None
        if synthesis_usage.output_tokens > synthesis_request.max_output_tokens:
            _logger.info(
                "owner_chat_operational_synthesis outcome=invalid "
                "schema=response_only fallback_reason=output_token_limit"
            )
            return (
                _operational_synthesis_fallback(
                    executed.output,
                    aggregate_usage,
                    result.provider_identifier,
                    result.model_identifier,
                ),
                aggregate_usage,
            )
        aggregate_usage = _add_usage(aggregate_usage, synthesis_usage)
        _logger.info(
            "owner_chat_operational_synthesis outcome=final schema=response_only"
        )
        return (
            _operational_result_with_usage(synthesis, aggregate_usage),
            aggregate_usage,
        )

    raise RuntimeError("Operational provider planner exited unexpectedly.")


def _validate_result(result: object, request: OwnerChatRequest) -> OwnerChatResult:
    if not isinstance(result, OwnerChatResult):
        raise OwnerChatProviderInvalidResponse
    if request.mode == "operational":
        expected_entity_kind = {
            "inventory_product": "product",
            "inventory_category": "category",
        }.get(result.semantic_operation)
        if result.semantic_operation is None or (
            expected_entity_kind is not None
            and (
                result.entity_kind != expected_entity_kind
                or not isinstance(result.entity_query, str)
                or not result.entity_query.strip()
            )
        ):
            raise OwnerChatProviderInvalidResponse(
                reason="invalid_operational_semantics"
            )
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
        elif result.decision == "set_preference":
            if (
                result.reply
                or result.tool_name is not None
                or result.tool_arguments is not None
                or result.preference_key != "default_inventory_location"
                or not isinstance(result.location_reference, str)
                or not result.location_reference.strip()
            ):
                raise OwnerChatProviderInvalidResponse(
                    reason="invalid_operational_response"
                )
        elif result.decision == "clear_preference":
            if (
                result.reply
                or result.tool_name is not None
                or result.tool_arguments is not None
                or result.preference_key != "default_inventory_location"
                or result.location_reference is not None
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
    elif request.mode == "operational_synthesis":
        if (
            result.proposed_knowledge
            or result.cited_source_ids
            or result.requires_business_knowledge
            or result.decision != "final"
            or result.tool_name is not None
            or result.tool_arguments is not None
            or result.validated_result_status != request.validated_result_status
            or not isinstance(result.reply, str)
            or not 1 <= len(result.reply.strip()) <= 14_000
        ):
            raise OwnerChatProviderInvalidResponse(
                reason="invalid_operational_synthesis_response"
            )
        if _is_unsafe_reply(result.reply):
            raise OwnerChatProviderInvalidResponse(reason="unsafe_output")
    elif request.mode == "category_resolution":
        candidate_references = {
            candidate.external_category_id for candidate in request.category_candidates
        }
        result_references = result.category_candidate_references
        status = result.category_resolution_status
        if (
            result.reply
            or result.proposed_knowledge
            or result.cited_source_ids
            or result.requires_business_knowledge
            or result.decision != "final"
            or result.tool_name is not None
            or result.tool_arguments is not None
            or result.preference_key is not None
            or result.location_reference is not None
            or result.semantic_operation is not None
            or result.entity_kind is not None
            or result.entity_query is not None
            or result.category_candidate_reference is not None
            or result.validated_result_status is not None
            or status not in {"matched", "ambiguous", "no_match"}
            or not isinstance(result_references, tuple)
            or any(
                not isinstance(reference, str) or reference not in candidate_references
                for reference in result_references
            )
            or len(set(result_references)) != len(result_references)
            or (status == "matched" and len(result_references) != 1)
            or (status == "ambiguous" and len(result_references) < 2)
            or (status == "no_match" and result_references)
        ):
            raise OwnerChatProviderInvalidResponse(
                reason="invalid_category_resolution_response"
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
            location_candidates = tuple(
                ProviderLocationCandidate(
                    label=candidate.label,
                    location_type=candidate.location_type,
                )
                for candidate in executor.location_candidates(user, business_id)
            )
            initial = _build_operational_request(
                session,
                business_id,
                claim.message_id,
                settings,
                provider_definitions,
                category_candidates=category_candidates,
                location_candidates=location_candidates,
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
                location_candidates,
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
                location_candidates = tuple(
                    ProviderLocationCandidate(
                        label=candidate.label,
                        location_type=candidate.location_type,
                    )
                    for candidate in operational_executor.location_candidates(
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
                    location_candidates=location_candidates,
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
                    location_candidates,
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
    except Exception as exc:
        _logger.info(
            "owner_chat_operational_dispatch outcome=unexpected_error type=%s",
            type(exc).__name__,
        )
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
