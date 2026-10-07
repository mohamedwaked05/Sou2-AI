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
from zoneinfo import ZoneInfo

from fastapi import status
from pydantic import ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
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
    ProviderPreferenceCapability,
    ProviderPreferenceLocationCandidate,
    ProviderProductCandidate,
    ProviderSource,
    ProviderToolDefinition,
    ProviderToolResult,
    ProviderWorkingDay,
    ProviderWorkingShift,
    TokenUsage,
    estimate_utf8_tokens,
    normalize_legacy_operational_preference,
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
    PendingOwnerOperationalPreference,
    User,
    UserOperationalPreference,
)
from app.integrations.profiles import ConnectionProfileRegistry
from app.rag.embeddings import create_embedding_provider
from app.rag.retrieval import retrieve
from app.schemas.operational import (
    BestSellingProductsResult,
    CategoryCandidate,
    InventoryQuery,
    InventoryResult,
    LocationCandidate,
    MetricCapabilityResult,
    PendingInventoryClarification,
    ProductResolutionCandidate,
    RestockingRecommendationsResult,
    SalesSummary,
)
from app.schemas.owner_chat import (
    ChatMessageResponse,
    ConversationHistoryResponse,
    OwnerMessageRequest,
    OwnerTurnResponse,
)
from app.schemas.source_mapping import (
    CatalogueProduct,
    CatalogueRequest,
    CatalogueResult,
    PendingCatalogueClarification,
)
from app.services.ai_usage import (
    AIUsageReservationClaim,
    reconcile_ai_usage,
    reserve_owner_chat_usage,
    resize_owner_chat_usage,
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
    PRODUCT_SEARCH_TOOL,
    RESTOCKING_RECOMMENDATIONS_TOOL,
    SALES_SUMMARY_TOOL,
    OperationalToolExecutor,
    OperationalToolResult,
    ToolExecutionError,
    _contains_control_payload,
)

_logger = logging.getLogger(__name__)

CHAT_CONTEXT_MESSAGE_LIMIT = 12
# A category inventory turn may need a planner, bounded resolver, and synthesis.
MAX_OPERATIONAL_PROVIDER_CALLS = 3
CATEGORY_RESOLUTION_MAX_OUTPUT_TOKENS = 64
PREFERENCE_RESOLUTION_MAX_OUTPUT_TOKENS = 64
PREFERENCE_PENDING_TTL = timedelta(minutes=15)
_SALES_CLARIFICATION_REPLY = (
    "Please specify the sales metric and a date range so I can run a bounded report."
)
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
_PREFERENCE_CAPABILITIES = (
    ProviderPreferenceCapability(
        preference_key="default_inventory_location",
        actions=("set_preference", "clear_preference"),
    ),
)


@dataclass(frozen=True)
class _Claim:
    message_id: uuid.UUID
    token: uuid.UUID


@dataclass(frozen=True)
class _InterpretedPreferenceIntent:
    """Typed planner intent that cannot write until bounded resolution succeeds."""

    action: str
    preference_key: str | None
    location_reference: str | None


@dataclass(frozen=True)
class _ValidatedPreferenceCommand:
    """The scoped persistence contract, built only from approved bounded values."""

    action: str
    preference_key: str
    location_external_id: str | None = None
    location_type: str | None = None


@dataclass(frozen=True)
class _PendingPreferenceCandidate:
    reference: str
    external_location_id: str
    location_type: str


@dataclass(frozen=True)
class _ValidatedOperationalCommand:
    """Backend-created operational command; provider proposals are never executable."""

    tool_name: str
    arguments: dict[str, object]
    provider_tool_fields: str
    consistency_outcome: str


@dataclass(frozen=True)
class _PreparedTurn:
    request: OwnerChatRequest
    business: Business
    has_usable_evidence: bool


@dataclass
class _OperationalUsageBudget:
    """Reserve actual stage payloads while holding all prior call consumption."""

    session: Session
    business: Business
    user: User
    claim: _Claim
    settings: Settings
    reservation: AIUsageReservationClaim | None = None
    usage: TokenUsage | None = None
    calls: int = 0

    def admit(self, provider: OwnerChatProvider, request: OwnerChatRequest) -> None:
        if self.calls >= MAX_OPERATIONAL_PROVIDER_CALLS:
            raise OwnerChatProviderInvalidResponse(
                reason="provider_call_limit", usage=self.usage, usage_uncertain=False
            )
        estimated_input = provider.estimate_input_tokens(request)
        if self.reservation is None:
            attempt = _admit_provider_generation(
                self.session, self.business.id, self.claim, self.settings
            )
            try:
                self.reservation = reserve_owner_chat_usage(
                    self.session,
                    business=self.business,
                    user=self.user,
                    owner_message_id=self.claim.message_id,
                    generation_attempt=attempt,
                    estimated_input_tokens=estimated_input,
                    max_output_tokens=request.max_output_tokens,
                    lease_seconds=self.settings.owner_chat_generation_lease_seconds,
                )
            except Exception:
                self.session.rollback()
                _undo_pre_provider_admission(
                    self.session, self.business.id, self.claim, attempt
                )
                raise
        else:
            assert self.usage is not None
            resize_owner_chat_usage(
                self.session,
                reservation=self.reservation,
                claim_token=self.claim.token,
                estimated_input_tokens=self.usage.input_tokens + estimated_input,
                max_output_tokens=self.usage.output_tokens + request.max_output_tokens,
            )
        self.calls += 1


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
    settings: Settings,
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
        settings=settings,
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
    user: User | None = None,
    executor: OperationalToolExecutor | None = None,
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
        pending_clarification=(
            _operational_pending_context(
                session, executor, user, business_id, owner_message
            )
            if executor is not None and user is not None
            else None
        ),
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
        pending_clarification=None,
    )


def _operational_synthesis_status(output: object) -> str:
    """Classify backend-validated tool output for response-only verification."""

    if isinstance(output, CatalogueResult):
        return "data" if output.status == "resolved" else output.status
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
    elif request.mode == "preference_resolution":
        output = json.dumps(
            {
                "status": result.preference_resolution_status,
                "preference_key": result.preference_resolution_key,
                "location_candidate_references": (
                    result.preference_location_candidate_references
                ),
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


def _explicit_sales_metric(message: str) -> str | None:
    """Normalize explicit metric names, never infer revenue from generic sales."""
    text = _normalized_classifier_text(message)
    names = {
        "revenue": r"\b(?:revenue|revenues)\b|(?:الإيرادات|الايرادات)",
        "gross_profit": r"\bgross profit\b|(?:الربح الإجمالي|الربح الاجمالي)",
        "net_profit": r"\bnet profit\b|(?:صافي الربح)",
        "sales_count": r"\b(?:sales count|sale count|number of sales)\b",
        "inventory_value": r"\binventory value\b",
    }
    matches = [metric for metric, pattern in names.items() if re.search(pattern, text)]
    return matches[0] if len(matches) == 1 else None


def _previous_completed_month(request: OwnerChatRequest) -> tuple[str, str]:
    local_date = request.requested_at.astimezone(
        ZoneInfo(request.reporting_timezone or request.profile.timezone)
    ).date()
    end = local_date.replace(day=1)
    start = (end - timedelta(days=1)).replace(day=1)
    return start.isoformat(), end.isoformat()


def _completed_month_period(
    message: str, request: OwnerChatRequest
) -> tuple[str, str] | None:
    text = _normalized_classifier_text(message)
    if re.search(
        r"\b(?:last month|previous month|latest completed month|last completed month)\b"
        r"|(?:الشهر الماضي|الشهر السابق)|\b(?:el shahr el made|shaher el made)\b",
        text,
    ):
        return _previous_completed_month(request)
    return None


def _pending_sales_clarification(
    session: Session,
    owner_message_id: uuid.UUID,
    requested_at: datetime,
    source_updated_at: datetime | None,
) -> bool:
    """Only an immediate, recent backend sales clarification may be continued.

    No earlier filters, locations, metrics, or message text enter the planner.
    The current follow-up must itself select a metric and bounded range.
    """
    owner = session.get(OwnerChatMessage, owner_message_id)
    if owner is None:
        return False
    assistant = session.scalar(
        select(OwnerChatMessage).where(
            OwnerChatMessage.conversation_id == owner.conversation_id,
            OwnerChatMessage.sequence_number == owner.sequence_number - 1,
            OwnerChatMessage.role == ChatMessageRole.ASSISTANT,
        )
    )
    return bool(
        assistant is not None
        and source_updated_at is not None
        and source_updated_at <= assistant.created_at
        and assistant.content.startswith(_SALES_CLARIFICATION_REPLY)
        and timedelta(0)
        <= requested_at - assistant.created_at
        <= PREFERENCE_PENDING_TTL
    )


def _validated_provider_arguments(
    executor: OperationalToolExecutor, tool_name: str, arguments: object
) -> dict[str, object] | None:
    """Validate a proposal without retaining it as an executable command."""

    if not isinstance(arguments, dict):
        return None
    definition = executor.registry[tool_name]
    input_schema = definition.provider_input_schema or definition.input_schema
    try:
        encoded = json.dumps(arguments, ensure_ascii=False, allow_nan=False)
        validated = input_schema.model_validate_json(encoded, strict=True)
    except TypeError, ValueError:
        return None
    return validated.model_dump(mode="json", exclude_none=True)


def _backend_operational_command(
    result: OwnerChatResult,
    executor: OperationalToolExecutor,
    category_candidates: tuple[ProviderCategoryCandidate, ...],
    request: OwnerChatRequest | None = None,
) -> _ValidatedOperationalCommand | None:
    """Derive one allowlisted command from semantic intent and typed inputs only."""

    tool_by_operation = {
        "product_search": PRODUCT_SEARCH_TOOL,
        "inventory_product": CURRENT_INVENTORY_TOOL,
        "inventory_category": CURRENT_INVENTORY_TOOL,
        "inventory_list": CURRENT_INVENTORY_TOOL,
        "restocking": RESTOCKING_RECOMMENDATIONS_TOOL,
        "sales_summary": SALES_SUMMARY_TOOL,
        "best_selling_products": BEST_SELLING_PRODUCTS_TOOL,
    }
    tool_name = tool_by_operation.get(result.semantic_operation)
    if tool_name is None:
        return None

    if result.tool_name is not None and result.tool_name not in executor.registry:
        raise OwnerChatProviderInvalidResponse(reason="prohibited_provider_tool")
    if result.tool_arguments is not None and _contains_control_payload(
        result.tool_arguments
    ):
        raise OwnerChatProviderInvalidResponse(reason="prohibited_provider_arguments")

    proposal_fields = (
        "present"
        if result.tool_name is not None or result.tool_arguments is not None
        else "missing"
    )
    proposal = _validated_provider_arguments(executor, tool_name, result.tool_arguments)
    arguments: dict[str, object] = {}

    if tool_name == PRODUCT_SEARCH_TOOL:
        if proposal is None:
            return None
        arguments = proposal
    elif tool_name == CURRENT_INVENTORY_TOOL:
        # Only the typed semantic entity can scope an inventory lookup. A provider
        # may propose an owner-facing location reference, which is separately
        # resolved by _prepare_inventory_location_arguments below.
        if proposal is not None:
            limit = proposal.get("limit")
            location_reference = proposal.get("location_reference")
            if isinstance(limit, int) and not isinstance(limit, bool):
                arguments["limit"] = limit
            if isinstance(location_reference, str):
                arguments["location_reference"] = location_reference
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
    elif tool_name == RESTOCKING_RECOMMENDATIONS_TOOL:
        # Restocking without a typed product/category intent is intentionally a
        # bounded recommendation list; provider filters and source IDs are ignored.
        if proposal is not None:
            limit = proposal.get("limit")
            if isinstance(limit, int) and not isinstance(limit, bool):
                arguments["limit"] = limit
    else:
        # Sales has no safe unfiltered default: a strict typed metric and reporting
        # scope are required before the backend creates the command.
        proposal = dict(proposal or {})
        if proposal.get("use_pending_clarification") and (
            request is None or not request.pending_sales_clarification
        ):
            return None
        if request is not None:
            current = request.messages[-1].content
            metric = _explicit_sales_metric(current)
            if metric is not None:
                proposal["metric"] = metric
            period = _completed_month_period(current, request)
            if (
                period is None
                and proposal.get("date_range") == "previous_completed_month"
            ):
                period = _previous_completed_month(request)
            if period is not None:
                proposal["start_date"], proposal["end_date"] = period
        for field in ("start_date", "end_date", "metric"):
            value = proposal.get(field)
            if not isinstance(value, str) or not value:
                return None
            arguments[field] = value
        if proposal.get("branch_external_id") is not None:
            arguments["branch_external_id"] = proposal["branch_external_id"]
        if tool_name == BEST_SELLING_PRODUCTS_TOOL and "limit" in proposal:
            arguments["limit"] = proposal["limit"]

    proposal_matches = (
        result.decision == "tool"
        and result.tool_name == tool_name
        and proposal is not None
        and {
            key: value
            for key, value in proposal.items()
            if key in arguments or key == "location_reference"
        }
        == arguments
    )
    return _ValidatedOperationalCommand(
        tool_name=tool_name,
        arguments=arguments,
        provider_tool_fields=proposal_fields,
        consistency_outcome="accepted" if proposal_matches else "normalized",
    )


def _consistent_operational_plan(
    result: OwnerChatResult,
    executor: OperationalToolExecutor,
    category_candidates: tuple[ProviderCategoryCandidate, ...],
    request: OwnerChatRequest | None = None,
) -> tuple[OwnerChatResult, _ValidatedOperationalCommand | None]:
    """Replace an interpreted plan with the backend-created executable command."""

    command = _backend_operational_command(
        result, executor, category_candidates, request
    )
    if command is None:
        return result, None
    return (
        replace(
            result,
            decision="tool",
            reply="",
            tool_name=command.tool_name,
            tool_arguments=command.arguments,
            preference_key=None,
            location_reference=None,
        ),
        command,
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


def _validated_generation(
    provider: OwnerChatProvider,
    request: OwnerChatRequest,
    budget: _OperationalUsageBudget | None = None,
) -> OwnerChatResult:
    if budget is not None:
        budget.admit(provider, request)
    try:
        result = provider.generate(request)
        try:
            validated = _validate_result(result, request)
        except OwnerChatProviderError as exc:
            if isinstance(result, OwnerChatResult):
                exc.usage = result.usage
                exc.provider_identifier = result.provider_identifier
                exc.model_identifier = result.model_identifier
            raise
    except OwnerChatProviderError as exc:
        if (
            isinstance(exc, OwnerChatProviderUnavailable)
            and exc.reason == "rate_limited"
        ):
            exc.usage_uncertain = False
        if budget is not None:
            budget.usage = _add_usage(
                budget.usage, _provider_failure_usage(provider, request, exc)
            )
        raise
    if budget is not None:
        budget.usage = _add_usage(
            budget.usage, _usage_for_result(provider, request, validated)
        )
    return validated


def _provider_failure_usage(
    provider: OwnerChatProvider,
    request: OwnerChatRequest,
    failure: OwnerChatProviderError,
) -> TokenUsage:
    if failure.usage is not None:
        return failure.usage
    if not failure.usage_uncertain or (
        isinstance(failure, OwnerChatProviderUnavailable)
        and failure.reason == "rate_limited"
    ):
        return TokenUsage(0, 0, 0, authoritative=False)
    estimated_input = provider.estimate_input_tokens(request)
    _logger.info(
        "owner_chat_failed_call mode=%s provider_usage=unknown "
        "charge_basis=estimated_input_plus_output_limit estimated_input=%s "
        "output_limit=%s",
        request.mode,
        estimated_input,
        request.max_output_tokens,
    )
    return TokenUsage(
        estimated_input,
        request.max_output_tokens,
        estimated_input + request.max_output_tokens,
        authoritative=False,
    )


def _safe_operational_label(value: str | None, fallback: str) -> str:
    if value is None or _is_unsafe_reply(value) or _contains_control_payload(value):
        return fallback
    return value


def _reporting_period_text(output: SalesSummary | BestSellingProductsResult) -> str:
    period = output.period
    location = " for the requested branch" if output.branch_external_id else ""
    return (
        f"{period.start_date} to {period.end_date} (end excluded), "
        f"{period.source_timezone}{location}"
    )


def _unresolved_operational_reply(
    output: InventoryResult | RestockingRecommendationsResult,
) -> str | None:
    for kind, resolution in (
        ("product", output.resolution),
        ("category", output.category_resolution),
    ):
        if resolution is None or resolution.status == "resolved":
            continue
        if resolution.status == "not_found":
            return f"The requested {kind} was not found in the live catalogue."
        labels = "; ".join(
            _safe_operational_label(
                getattr(candidate, "name", getattr(candidate, "label", None)), kind
            )
            for candidate in resolution.candidates[:5]
        )
        return f"I found several matching {kind}s: {labels}. Which one do you mean?"
    return None


def _operational_synthesis_fallback(
    output: object,
    usage: TokenUsage,
    provider_identifier: str | None,
    model_identifier: str | None,
) -> OwnerChatResult:
    if isinstance(output, InventoryResult):
        unresolved = _unresolved_operational_reply(output)
        if unresolved is not None:
            reply = unresolved
        elif output.items:
            entries = []
            for item in output.items[:10]:
                location = _safe_operational_label(
                    item.branch_name or item.warehouse_name, "the source location"
                )
                entries.append(
                    f"{_safe_operational_label(item.product.name, 'Product')}: "
                    f"{item.available_quantity} available; "
                    f"{item.on_hand_quantity} on hand; "
                    f"{item.reserved_quantity} reserved "
                    f"at {location}"
                )
            reply = "Current inventory: " + "; ".join(entries) + "."
            reply += (
                " Quantities are source stock units; pack/case units were not supplied."
            )
            if len(output.items) > 10 or output.metadata.is_truncated:
                reply += (
                    f" Showing {min(10, len(output.items))} of {len(output.items)} "
                    "returned rows. Ask for a narrower product, category or location."
                )
                if output.metadata.is_truncated:
                    reply += (
                        " The source result is limited; more rows may be available."
                    )
        else:
            reply = "No matching inventory rows were returned."
    elif isinstance(output, SalesSummary):
        period = _reporting_period_text(output)
        if output.metric == "revenue":
            reply = (
                f"Sales for {period}: net revenue "
                f"{output.net_revenue} {output.currency}; "
                f"gross revenue {output.gross_revenue} {output.currency}; "
                f"refunds {output.refund_amount} {output.currency}."
            )
        elif output.metric == "sales_count":
            reply = (
                f"Sales count for {period}: "
                f"{output.completed_sale_count} completed sales; "
                f"{output.returned_sale_count} sales with returns; "
                f"{output.completed_refund_count} completed refunds."
            )
        elif output.metric in {"gross_profit", "net_profit"}:
            value = (
                output.gross_profit
                if output.metric == "gross_profit"
                else output.net_profit
            )
            reply = (
                f"{output.metric.replace('_', ' ').capitalize()} for {period}: "
                f"{value} {output.currency}."
            )
        else:
            reply = (
                "The validated sales result does not supply the requested "
                "inventory valuation."
            )
    elif isinstance(output, BestSellingProductsResult):
        entries = [
            f"{item.rank}. {_safe_operational_label(item.product.name, 'Product')}: "
            f"{item.quantity_sold} net units; "
            f"{item.revenue} {item.currency} net revenue"
            for item in output.items[:10]
        ]
        reply = f"Best sellers for {_reporting_period_text(output)}: "
        reply += "; ".join(entries) + "." if entries else "no matching sales."
        if len(output.items) > 10 or output.metadata.is_truncated:
            reply += (
                f" Showing {min(10, len(output.items))} rows; more may be available."
            )
    elif isinstance(output, RestockingRecommendationsResult):
        unresolved = _unresolved_operational_reply(output)
        if unresolved is not None:
            reply = unresolved
        elif output.items:
            entries = []
            for item in output.items[:10]:
                location = _safe_operational_label(
                    item.inventory.branch_name or item.inventory.warehouse_name,
                    "the source location",
                )
                entries.append(
                    f"{_safe_operational_label(item.inventory.product.name, 'Product')}"
                    ": "
                    f"restock {item.recommended_quantity} at {location} "
                    f"({item.inventory.available_quantity} available; "
                    f"target {item.inventory.target_stock})"
                )
            reply = "Restocking recommendations: " + "; ".join(entries) + "."
            if len(output.items) > 10 or output.metadata.is_truncated:
                reply += (
                    f" Showing {min(10, len(output.items))} rows; "
                    "more may be available."
                )
        else:
            reply = "No matching items require restocking under the source rules."
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
    elif isinstance(output, dict) and output.get("capability") == "inventory_location":
        candidates = output.get("candidates", [])
        reply = (
            "Which source location do you mean: "
            + "; ".join(
                _safe_operational_label(item.get("label"), "location")
                for item in candidates
            )
            + "?"
            if candidates
            else "No matching source location was found."
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
    *,
    owner_message: str | None = None,
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
    # A model-expanded label must not erase ambiguity in the owner's source name.
    # This validates source candidates only after the AI selected inventory.
    literal_locations = (
        _preference_locations_in_message(
            owner_message, tuple(executor.location_candidates(user, business_id))
        )
        if owner_message is not None
        else ()
    )
    if len(literal_locations) > 1:
        return _InventoryLocationPreparation(
            arguments=updated,
            result=ProviderToolResult(
                tool_name="inventory_location",
                output={
                    "capability": "inventory_location",
                    "status": "ambiguous",
                    "candidates": [
                        {
                            "label": candidate.label,
                            "location_type": candidate.location_type,
                        }
                        for candidate in literal_locations
                    ],
                },
            ),
            location_source="current_turn",
            location_input_kind="label",
            preference_loaded=False,
            preference_applied=False,
            location_resolution="multiple",
        )
    if len(literal_locations) == 1:
        current_turn_reference = literal_locations[0].external_location_id
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


def _supported_preference_keys(action: str) -> tuple[str, ...]:
    return tuple(
        capability.preference_key
        for capability in _PREFERENCE_CAPABILITIES
        if action in capability.actions
    )


def _interpret_preference_intent(
    result: OwnerChatResult,
) -> _InterpretedPreferenceIntent:
    if result.decision not in {"set_preference", "clear_preference"}:
        raise OwnerChatProviderInvalidResponse(reason="invalid_operational_response")
    if (
        result.semantic_operation != "preference"
        or result.reply
        or result.tool_name is not None
        or result.tool_arguments is not None
    ):
        raise OwnerChatProviderInvalidResponse(reason="invalid_operational_response")
    if (
        result.preference_key is not None
        and result.preference_key not in _supported_preference_keys(result.decision)
    ):
        raise OwnerChatProviderInvalidResponse(reason="invalid_operational_response")
    return _InterpretedPreferenceIntent(
        action=result.decision,
        preference_key=result.preference_key,
        location_reference=result.location_reference,
    )


def _resolved_preference_key(intent: _InterpretedPreferenceIntent) -> str | None:
    if intent.preference_key is not None:
        return intent.preference_key
    supported_keys = _supported_preference_keys(intent.action)
    return supported_keys[0] if len(supported_keys) == 1 else None


def _preference_location_candidates(
    candidates: tuple[object, ...],
) -> tuple[ProviderPreferenceLocationCandidate, ...]:
    resolved: list[ProviderPreferenceLocationCandidate] = []
    for index, candidate in enumerate(candidates, start=1):
        label = getattr(candidate, "label", None)
        location_type = getattr(candidate, "location_type", None)
        if not isinstance(label, str) or location_type not in {"branch", "warehouse"}:
            continue
        resolved.append(
            ProviderPreferenceLocationCandidate(
                reference=f"location_{index}", label=label, location_type=location_type
            )
        )
    return tuple(resolved)


def _preference_location_matches(
    location_reference: str | None,
    candidates: tuple[object, ...],
) -> tuple[object, ...]:
    if not isinstance(location_reference, str) or not location_reference.strip():
        return ()
    normalized_reference = _normalized_classifier_text(location_reference)
    return tuple(
        candidate
        for candidate in candidates
        if re.search(
            r"(?<!\w)" + re.escape(normalized_reference) + r"(?!\w)",
            _normalized_classifier_text(str(getattr(candidate, "label", ""))),
        )
    )


def _deterministic_preference_location(
    location_reference: str | None, candidates: tuple[object, ...]
) -> object | None:
    matches = _preference_location_matches(location_reference, candidates)
    return matches[0] if len(matches) == 1 else None


def _preference_locations_in_message(
    message: str, candidates: tuple[object, ...]
) -> tuple[object, ...]:
    """Find whole source labels (or location names) in a typed command."""
    normalized = _normalized_classifier_text(message)
    matches = []
    explicit_names = set()
    for candidate in candidates:
        label = _normalized_classifier_text(str(getattr(candidate, "label", "")))
        if label and re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", normalized):
            matches.append(candidate)
            explicit_names.add(re.sub(r"\s+(?:branch|warehouse)$", "", label))
    for candidate in candidates:
        label = _normalized_classifier_text(str(getattr(candidate, "label", "")))
        name = re.sub(r"\s+(?:branch|warehouse)$", "", label)
        if (
            name
            and name not in explicit_names
            and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", normalized)
        ):
            matches.append(candidate)
    return tuple(matches)


def _validated_preference_command(
    intent: _InterpretedPreferenceIntent,
    preference_key: str | None,
    location: object | None = None,
) -> _ValidatedPreferenceCommand | None:
    if preference_key not in _supported_preference_keys(intent.action):
        return None
    if intent.action == "clear_preference":
        return _ValidatedPreferenceCommand(
            action=intent.action,
            preference_key=preference_key,
            location_external_id=getattr(location, "external_location_id", None),
            location_type=getattr(location, "location_type", None),
        )
    external_id = getattr(location, "external_location_id", None)
    location_type = getattr(location, "location_type", None)
    if not isinstance(external_id, str) or location_type not in {"branch", "warehouse"}:
        return None
    return _ValidatedPreferenceCommand(
        action=intent.action,
        preference_key=preference_key,
        location_external_id=external_id,
        location_type=location_type,
    )


def _build_preference_resolution_request(
    request: OwnerChatRequest,
    candidates: tuple[ProviderPreferenceLocationCandidate, ...],
) -> OwnerChatRequest:
    """Create a compact resolver request with no chat, RAG, or business context."""

    return replace(
        request,
        messages=(request.messages[-1],),
        rolling_summary=None,
        max_output_tokens=min(
            request.max_output_tokens, PREFERENCE_RESOLUTION_MAX_OUTPUT_TOKENS
        ),
        mode="preference_resolution",
        tools=(),
        tool_results=(),
        category_candidates=(),
        location_candidates=(),
        pending_product_candidates=(),
        preference_capabilities=_PREFERENCE_CAPABILITIES,
        pending_clarification=None,
        preference_location_candidates=candidates,
    )


def _pending_candidate_records(
    pending: PendingOwnerOperationalPreference,
) -> tuple[_PendingPreferenceCandidate, ...]:
    records: list[_PendingPreferenceCandidate] = []
    for value in pending.candidate_references:
        if not isinstance(value, dict):
            continue
        reference = value.get("reference")
        external_id = value.get("external_location_id")
        location_type = value.get("location_type")
        if (
            isinstance(reference, str)
            and isinstance(external_id, str)
            and location_type in {"branch", "warehouse"}
        ):
            records.append(
                _PendingPreferenceCandidate(reference, external_id, location_type)
            )
    return tuple(records)


def _active_pending_preference(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    conversation_id: uuid.UUID,
    source_id: uuid.UUID | None,
) -> PendingOwnerOperationalPreference | None:
    pending_rows = session.scalars(
        select(PendingOwnerOperationalPreference)
        .where(
            PendingOwnerOperationalPreference.user_id == user.id,
            PendingOwnerOperationalPreference.business_id == business_id,
            PendingOwnerOperationalPreference.conversation_id == conversation_id,
            PendingOwnerOperationalPreference.state == "pending",
        )
        .with_for_update()
    ).all()
    now = utc_now()
    active: PendingOwnerOperationalPreference | None = None
    for pending in pending_rows:
        if pending.expires_at <= now:
            pending.state = "expired"
            pending.version += 1
        elif source_id is None or pending.source_id != source_id:
            pending.state = "invalidated"
            pending.version += 1
        else:
            active = pending
    if any(item.state != "pending" for item in pending_rows):
        session.commit()
    return active


def _store_pending_preference(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    owner_message_id: uuid.UUID,
    conversation_id: uuid.UUID,
    candidates: tuple[object, ...],
) -> PendingOwnerOperationalPreference:
    references = [
        {
            "reference": f"location_{index}",
            "external_location_id": str(candidate.external_location_id),
            "location_type": str(candidate.location_type),
        }
        for index, candidate in enumerate(candidates, start=1)
        if isinstance(getattr(candidate, "external_location_id", None), str)
        and getattr(candidate, "location_type", None) in {"branch", "warehouse"}
    ]
    if not references:
        raise ToolExecutionError("invalid_arguments")
    now = utc_now()
    statement = insert(PendingOwnerOperationalPreference).values(
        id=uuid.uuid4(),
        user_id=user.id,
        business_id=business_id,
        source_id=source_id,
        conversation_id=conversation_id,
        originating_message_id=owner_message_id,
        operation="set_preference",
        preference_key="default_inventory_location",
        expected_field="location",
        candidate_references=references,
        state="pending",
        version=1,
        expires_at=now + PREFERENCE_PENDING_TTL,
    )
    statement = statement.on_conflict_do_update(
        index_elements=(
            "user_id",
            "business_id",
            "source_id",
            "conversation_id",
            "preference_key",
        ),
        index_where=PendingOwnerOperationalPreference.state == "pending",
        set_={
            "originating_message_id": owner_message_id,
            "candidate_references": references,
            "expected_field": "location",
            "version": PendingOwnerOperationalPreference.version + 1,
            "expires_at": now + PREFERENCE_PENDING_TTL,
            "updated_at": now,
        },
    )
    session.execute(statement)
    pending = session.scalar(
        select(PendingOwnerOperationalPreference)
        .where(
            PendingOwnerOperationalPreference.user_id == user.id,
            PendingOwnerOperationalPreference.business_id == business_id,
            PendingOwnerOperationalPreference.source_id == source_id,
            PendingOwnerOperationalPreference.conversation_id == conversation_id,
            PendingOwnerOperationalPreference.preference_key
            == "default_inventory_location",
            PendingOwnerOperationalPreference.state == "pending",
        )
        .with_for_update()
    )
    if pending is None:  # pragma: no cover - guarded by the partial unique index
        raise ToolExecutionError("invalid_arguments")
    return pending


def _finish_pending_preference(
    pending: PendingOwnerOperationalPreference | None, state: str
) -> None:
    if pending is not None and pending.state == "pending":
        pending.state = state
        pending.version += 1


def _supersede_pending_preference(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    conversation_id: uuid.UUID,
    source_id: uuid.UUID,
) -> None:
    pending = _active_pending_preference(
        session, user, business_id, conversation_id, source_id
    )
    _finish_pending_preference(pending, "superseded")


def _save_inventory_location_preference(
    session: Session,
    executor: OperationalToolExecutor,
    user: User,
    business_id: uuid.UUID,
    command: _ValidatedPreferenceCommand,
    pending: PendingOwnerOperationalPreference | None = None,
) -> ProviderToolResult:
    source = executor._active_source(business_id)
    if source is None:
        raise ToolExecutionError("integration_unavailable")
    statement = select(UserOperationalPreference).where(
        UserOperationalPreference.user_id == user.id,
        UserOperationalPreference.business_id == business_id,
        UserOperationalPreference.source_id == source.id,
        UserOperationalPreference.preference_key == command.preference_key,
    )
    existing = session.scalar(statement.with_for_update())
    if command.action == "clear_preference":
        matches_requested_location = command.location_external_id is None or (
            existing is not None
            and existing.location_external_id == command.location_external_id
            and existing.location_type == command.location_type
        )
        if existing is not None and matches_requested_location:
            session.delete(existing)
            _finish_pending_preference(pending, "completed")
            session.commit()
            action = "cleared"
        elif existing is not None:
            _finish_pending_preference(pending, "completed")
            session.commit()
            action = "no_matching_saved_preference"
        else:
            _finish_pending_preference(pending, "completed")
            session.commit()
            action = "no_saved_preference"
        return ProviderToolResult(
            tool_name="inventory_location_preference",
            output={"action": action, "capability": "inventory_location_preference"},
        )
    if command.location_external_id is None or command.location_type is None:
        raise ToolExecutionError("invalid_arguments")
    candidates = executor.location_candidates(user, business_id)
    current_source = executor._active_source(business_id)
    if current_source is None or current_source.id != source.id:
        raise ToolExecutionError("integration_unavailable")
    location = next(
        (
            candidate
            for candidate in candidates
            if candidate.external_location_id == command.location_external_id
            and candidate.location_type == command.location_type
        ),
        None,
    )
    if location is None:
        raise ToolExecutionError("invalid_arguments")
    if existing is None:
        inserted = session.scalar(
            insert(UserOperationalPreference)
            .values(
                id=uuid.uuid4(),
                user_id=user.id,
                business_id=business_id,
                source_id=source.id,
                preference_key=command.preference_key,
                location_type=location.location_type,
                location_external_id=location.external_location_id,
            )
            .on_conflict_do_nothing(
                index_elements=("user_id", "business_id", "source_id", "preference_key")
            )
            .returning(UserOperationalPreference.id)
        )
        if inserted is not None:
            action = "created"
        else:
            existing = session.scalar(statement.with_for_update())
            if existing is None:  # pragma: no cover - conflict row must be visible
                raise ToolExecutionError("persistence_failed")
            action = (
                "already_set"
                if existing.location_type == location.location_type
                and existing.location_external_id == location.external_location_id
                else "replaced"
            )
    else:
        action = (
            "already_set"
            if existing.location_type == location.location_type
            and existing.location_external_id == location.external_location_id
            else "replaced"
        )
    if existing is not None and action == "replaced":
        existing.location_type = location.location_type
        existing.location_external_id = location.external_location_id
    _finish_pending_preference(pending, "completed")
    session.commit()
    return ProviderToolResult(
        tool_name="inventory_location_preference",
        output={
            "action": action,
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


def _preference_resolution_reply(
    status: str,
    references: tuple[str, ...],
    candidates: tuple[ProviderPreferenceLocationCandidate, ...],
) -> str:
    if status == "ambiguous":
        labels = tuple(
            candidate.label
            for candidate in candidates
            if candidate.reference in references
        )
        if len(labels) >= 2:
            return (
                f"I found several locations: {'; '.join(labels[:5])}. "
                "Which one do you mean?"
            )
    if status == "no_match":
        return (
            "I couldn't match that preference to an available location. "
            "Please choose one."
        )
    return "I couldn't safely set that preference. Please choose an available location."


def _preference_acknowledgement(action: str) -> str:
    replies = {
        "created": "Your default inventory location was saved.",
        "replaced": "Your default inventory location was replaced.",
        "already_set": "That inventory location is already your default.",
        "cleared": "Your default inventory location was cleared.",
        "no_saved_preference": "No default inventory location was saved.",
        "no_matching_saved_preference": (
            "No matching saved inventory location was found."
        ),
    }
    return replies.get(action, "I couldn't safely save that preference.")


def _pending_resolver_candidates(
    pending: PendingOwnerOperationalPreference,
    source_candidates: tuple[object, ...],
) -> tuple[tuple[ProviderPreferenceLocationCandidate, ...], dict[str, object]]:
    source_by_identity = {
        (
            getattr(candidate, "external_location_id", None),
            getattr(candidate, "location_type", None),
        ): candidate
        for candidate in source_candidates
    }
    candidates: list[ProviderPreferenceLocationCandidate] = []
    candidate_by_reference: dict[str, object] = {}
    for record in _pending_candidate_records(pending):
        candidate = source_by_identity.get(
            (record.external_location_id, record.location_type)
        )
        label = getattr(candidate, "label", None)
        if not isinstance(label, str):
            continue
        candidates.append(
            ProviderPreferenceLocationCandidate(
                reference=record.reference,
                label=label,
                location_type=record.location_type,
            )
        )
        candidate_by_reference[record.reference] = candidate
    return tuple(candidates), candidate_by_reference


def _preference_result(
    output: ProviderToolResult,
    usage: TokenUsage | None,
    provider_identifier: str | None,
    model_identifier: str | None,
) -> tuple[OwnerChatResult, TokenUsage | None]:
    action = output.output.get("action") if isinstance(output.output, dict) else None
    return (
        OwnerChatResult(
            reply=_preference_acknowledgement(
                action if isinstance(action, str) else ""
            ),
            usage=usage,
            provider_identifier=provider_identifier,
            model_identifier=model_identifier,
        ),
        usage,
    )


def _run_preference_intent(
    session: Session,
    business_id: uuid.UUID,
    claim: _Claim,
    user: User,
    provider: OwnerChatProvider,
    request: OwnerChatRequest,
    result: OwnerChatResult,
    aggregate_usage: TokenUsage,
    executor: OperationalToolExecutor,
    budget: _OperationalUsageBudget | None = None,
) -> tuple[OwnerChatResult, TokenUsage]:
    intent = _interpret_preference_intent(result)
    source = executor._active_source(business_id)
    if source is None:
        return (
            OwnerChatResult(
                reply="The connected inventory source is unavailable.",
                usage=aggregate_usage,
                provider_identifier=result.provider_identifier,
                model_identifier=result.model_identifier,
            ),
            aggregate_usage,
        )
    owner_message = session.get(OwnerChatMessage, claim.message_id)
    if owner_message is None:  # pragma: no cover - claim invariants protect this
        raise ToolExecutionError("invalid_arguments")
    _supersede_pending_preference(
        session, user, business_id, owner_message.conversation_id, source.id
    )
    preference_key = _resolved_preference_key(intent)
    source_candidates = tuple(executor.location_candidates(user, business_id))
    literal_matches = _preference_location_matches(
        intent.location_reference, source_candidates
    )
    if intent.action == "set_preference":
        owner_matches = _preference_locations_in_message(
            owner_message.content, source_candidates
        )
        if owner_matches:
            literal_matches = owner_matches
    location = literal_matches[0] if len(literal_matches) == 1 else None
    command = _validated_preference_command(intent, preference_key, location)
    if command is not None and command.action == "clear_preference":
        if intent.location_reference is not None and location is None:
            return (
                OwnerChatResult(
                    reply="No matching saved inventory location was found.",
                    usage=aggregate_usage,
                    provider_identifier=result.provider_identifier,
                    model_identifier=result.model_identifier,
                ),
                aggregate_usage,
            )
        saved = _save_inventory_location_preference(
            session, executor, user, business_id, command
        )
        response, usage = _preference_result(
            saved, aggregate_usage, result.provider_identifier, result.model_identifier
        )
        assert usage is not None
        return response, usage

    command = _validated_preference_command(intent, preference_key, location)
    if command is not None:
        saved = _save_inventory_location_preference(
            session, executor, user, business_id, command
        )
        response, usage = _preference_result(
            saved, aggregate_usage, result.provider_identifier, result.model_identifier
        )
        assert usage is not None
        return response, usage

    if len(literal_matches) > 1:
        return _clarify_literal_preference_matches(
            session,
            user,
            business_id,
            source.id,
            owner_message,
            literal_matches,
            aggregate_usage,
            result,
        ), aggregate_usage

    resolver_candidates = _preference_location_candidates(source_candidates)
    resolved_location: object | None = None
    resolution_status = "invalid"
    resolution_references: tuple[str, ...] = ()
    if resolver_candidates:
        resolver_request = _build_preference_resolution_request(
            request, resolver_candidates
        )
        try:
            resolver_result = _validated_generation(provider, resolver_request, budget)
        except OwnerChatProviderError as exc:
            aggregate_usage = _add_usage(
                aggregate_usage,
                _provider_failure_usage(provider, resolver_request, exc),
            )
            resolver_result = None
        if resolver_result is not None:
            aggregate_usage = _add_usage(
                aggregate_usage,
                _usage_for_result(provider, resolver_request, resolver_result),
            )
            resolution_status = (
                resolver_result.preference_resolution_status or "invalid"
            )
            resolution_references = (
                resolver_result.preference_location_candidate_references
            )
            if (
                resolver_result.preference_resolution_status == "matched"
                and resolver_result.preference_resolution_key == preference_key
                and len(resolver_result.preference_location_candidate_references) == 1
            ):
                by_reference = {
                    candidate.reference: source_candidate
                    for candidate, source_candidate in zip(
                        resolver_candidates, source_candidates, strict=True
                    )
                }
                resolved_location = by_reference.get(
                    resolver_result.preference_location_candidate_references[0]
                )
    command = _validated_preference_command(intent, preference_key, resolved_location)
    if command is not None:
        saved = _save_inventory_location_preference(
            session, executor, user, business_id, command
        )
        response, usage = _preference_result(
            saved, aggregate_usage, result.provider_identifier, result.model_identifier
        )
        assert usage is not None
        return response, usage
    selected_candidates = (
        tuple(
            candidate
            for index, candidate in enumerate(source_candidates, start=1)
            if f"location_{index}" in resolution_references
        )
        if resolution_status == "ambiguous"
        else source_candidates
    )
    _store_pending_preference(
        session,
        user,
        business_id,
        source.id,
        claim.message_id,
        owner_message.conversation_id,
        selected_candidates,
    )
    session.commit()
    return (
        OwnerChatResult(
            reply=_preference_resolution_reply(
                resolution_status, resolution_references, resolver_candidates
            ),
            usage=aggregate_usage,
            provider_identifier=result.provider_identifier,
            model_identifier=result.model_identifier,
        ),
        aggregate_usage,
    )


def _clarify_literal_preference_matches(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    owner_message: OwnerChatMessage,
    matches: tuple[object, ...],
    usage: TokenUsage,
    result: OwnerChatResult,
) -> OwnerChatResult:
    """A provider cannot reduce a known source ambiguity to an arbitrary match."""
    _store_pending_preference(
        session,
        user,
        business_id,
        source_id,
        owner_message.id,
        owner_message.conversation_id,
        matches,
    )
    session.commit()
    candidates = _preference_location_candidates(matches)
    return OwnerChatResult(
        reply=_preference_resolution_reply(
            "ambiguous",
            tuple(candidate.reference for candidate in candidates),
            candidates,
        ),
        usage=usage,
        provider_identifier=result.provider_identifier,
        model_identifier=result.model_identifier,
    )


def _validated_pending_preference(
    session: Session,
    executor: OperationalToolExecutor,
    user: User,
    business_id: uuid.UUID,
    owner: OwnerChatMessage,
) -> (
    tuple[
        PendingOwnerOperationalPreference,
        tuple[ProviderPreferenceLocationCandidate, ...],
        dict[str, object],
    ]
    | None
):
    """Revalidate scope and the last completed clarification, skipping failed turns."""
    source = executor._active_source(business_id)
    pending = _active_pending_preference(
        session, user, business_id, owner.conversation_id, source.id if source else None
    )
    if pending is None or source is None:
        return None
    origin = session.get(OwnerChatMessage, pending.originating_message_id)
    reply = session.scalar(
        select(OwnerChatMessage).where(
            OwnerChatMessage.conversation_id == owner.conversation_id,
            OwnerChatMessage.reply_to_message_id == pending.originating_message_id,
            OwnerChatMessage.role == ChatMessageRole.ASSISTANT,
        )
    )
    if (
        origin is None
        or origin.conversation_id != owner.conversation_id
        or origin.generation_state != ChatGenerationState.COMPLETED
        or origin.sequence_number >= owner.sequence_number
        or reply is None
    ):
        _finish_pending_preference(pending, "invalidated")
        session.commit()
        return None
    intervening = session.scalar(
        select(OwnerChatMessage.id)
        .where(
            OwnerChatMessage.conversation_id == owner.conversation_id,
            OwnerChatMessage.role == ChatMessageRole.OWNER,
            OwnerChatMessage.generation_state == ChatGenerationState.COMPLETED,
            OwnerChatMessage.sequence_number > origin.sequence_number,
            OwnerChatMessage.sequence_number < owner.sequence_number,
        )
        .limit(1)
    )
    if intervening is not None:
        _finish_pending_preference(pending, "superseded")
        session.commit()
        return None
    candidates, by_reference = _pending_resolver_candidates(
        pending, tuple(executor.location_candidates(user, business_id))
    )
    if (
        source.updated_at > reply.created_at
        or not candidates
        or len(candidates) != len(_pending_candidate_records(pending))
    ):
        _finish_pending_preference(pending, "invalidated")
        session.commit()
        return None
    return pending, candidates, by_reference


def _complete_pending_preference_if_selected(
    session: Session,
    business_id: uuid.UUID,
    claim: _Claim,
    user: User,
    provider: OwnerChatProvider,
    request: OwnerChatRequest,
    result: OwnerChatResult,
    aggregate_usage: TokenUsage,
    executor: OperationalToolExecutor,
) -> tuple[OwnerChatResult | None, TokenUsage] | None:
    owner_message = session.get(OwnerChatMessage, claim.message_id)
    if owner_message is None:
        return None
    validated = _validated_pending_preference(
        session, executor, user, business_id, owner_message
    )
    if validated is None:
        return None
    pending, resolver_candidates, candidate_by_reference = validated
    if result.pending_reply in {"cancel", "replace", "unrelated"}:
        _validate_pending_preference_reply(result, request)
        _finish_pending_preference(pending, "superseded")
        session.commit()
        if result.pending_reply == "cancel":
            return OwnerChatResult(
                reply="The pending inventory-location choice was cancelled.",
                usage=aggregate_usage,
                provider_identifier=result.provider_identifier,
                model_identifier=result.model_identifier,
            ), aggregate_usage
        return None
    query = result.location_reference or (result.tool_arguments or {}).get(
        "location_reference"
    )
    location = (
        _source_candidate_selection(
            owner_message.content, query, tuple(candidate_by_reference.values())
        )
        if result.pending_reply == "selection"
        else None
    )
    if location is None:
        # Advance the completed clarification boundary without renewing its expiry.
        pending.originating_message_id = owner_message.id
        pending.version += 1
        session.commit()
        return OwnerChatResult(
            reply=_preference_resolution_reply(
                "ambiguous" if len(resolver_candidates) > 1 else "no_match",
                tuple(item.reference for item in resolver_candidates),
                resolver_candidates,
            ),
            usage=aggregate_usage,
            provider_identifier=result.provider_identifier,
            model_identifier=result.model_identifier,
        ), aggregate_usage
    command = _validated_preference_command(
        _InterpretedPreferenceIntent(
            action="set_preference",
            preference_key=pending.preference_key,
            location_reference=None,
        ),
        pending.preference_key,
        location,
    )
    if command is None:
        return None, aggregate_usage
    saved = _save_inventory_location_preference(
        session, executor, user, business_id, command, pending
    )
    response, usage = _preference_result(
        saved, aggregate_usage, result.provider_identifier, result.model_identifier
    )
    assert usage is not None
    return response, usage


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


def _previous_product_clarification(
    session: Session,
    owner_message_id: uuid.UUID,
) -> dict[str, object] | None:
    """Read only the immediately preceding, scoped backend clarification."""

    owner_message = session.get(OwnerChatMessage, owner_message_id)
    if owner_message is None or owner_message.sequence_number < 3:
        return None
    previous_owner = session.scalar(
        select(OwnerChatMessage).where(
            OwnerChatMessage.conversation_id == owner_message.conversation_id,
            OwnerChatMessage.sequence_number == owner_message.sequence_number - 2,
            OwnerChatMessage.role == ChatMessageRole.OWNER,
            OwnerChatMessage.generation_state == ChatGenerationState.COMPLETED,
        )
    )
    if previous_owner is None:
        # A failed turn has no answer and must not consume a catalogue choice.
        previous_owner = session.scalar(
            select(OwnerChatMessage)
            .where(
                OwnerChatMessage.conversation_id == owner_message.conversation_id,
                OwnerChatMessage.sequence_number < owner_message.sequence_number,
                OwnerChatMessage.role == ChatMessageRole.OWNER,
                OwnerChatMessage.generation_state == ChatGenerationState.COMPLETED,
            )
            .order_by(OwnerChatMessage.sequence_number.desc())
            .limit(1)
        )
        if (
            previous_owner is None
            or (previous_owner.operational_clarification or {}).get("operation")
            != PRODUCT_SEARCH_TOOL
        ):
            return None
    previous_assistant = session.scalar(
        select(OwnerChatMessage).where(
            OwnerChatMessage.conversation_id == owner_message.conversation_id,
            OwnerChatMessage.sequence_number == previous_owner.sequence_number + 1
            if previous_owner is not None
            else False,
            OwnerChatMessage.role == ChatMessageRole.ASSISTANT,
            OwnerChatMessage.reply_to_message_id == previous_owner.id
            if previous_owner is not None
            else False,
        )
    )
    if (
        previous_owner is None
        or previous_assistant is None
        or previous_owner.operational_clarification is None
    ):
        return None
    return previous_owner.operational_clarification


def _pending_product_clarification(
    session: Session,
    executor: OperationalToolExecutor,
    user: User,
    business_id: uuid.UUID,
    owner_message_id: uuid.UUID,
) -> PendingInventoryClarification | PendingCatalogueClarification | None:
    state = _previous_product_clarification(session, owner_message_id)
    if state is None:
        return None
    owner_message = session.get(OwnerChatMessage, owner_message_id)
    try:
        contract = (
            PendingCatalogueClarification
            if state.get("operation") == PRODUCT_SEARCH_TOOL
            else PendingInventoryClarification
        )
        pending = contract.model_validate(state)
    except ValidationError:
        return None
    source = executor._active_source(business_id)
    conversation = session.get(OwnerConversation, owner_message.conversation_id)
    if (
        source is None
        or conversation is None
        or conversation.business_id != business_id
        or pending.user_id != user.id
        or pending.source_id != source.id
        or pending.source_updated_at != source.updated_at
        or pending.expires_at <= utc_now()
    ):
        return None
    if isinstance(pending, PendingCatalogueClarification):
        from app.integrations.discovery import SourceMappingError
        from app.services.source_mapping import mapped_source

        if (
            pending.business_id != business_id
            or pending.conversation_id != owner_message.conversation_id
        ):
            return None
        try:
            adapter = mapped_source(session, executor._profiles, source)
        except SourceMappingError:
            return None
        if (
            pending.mapping_version != adapter.revision_version
            or pending.schema_fingerprint != adapter.discovery.schema_fingerprint
            or pending.source_fingerprint != adapter.discovery.source_fingerprint
        ):
            return None
    return pending


def _operational_pending_context(
    session: Session,
    executor: OperationalToolExecutor,
    user: User,
    business_id: uuid.UUID,
    owner: OwnerChatMessage,
) -> dict[str, object] | None:
    """Expose only an immediate, scoped clarification, never arbitrary history."""
    preference = _validated_pending_preference(
        session, executor, user, business_id, owner
    )
    if preference is not None:
        pending, candidates, _ = preference
        origin = session.get(OwnerChatMessage, pending.originating_message_id)
        question = session.scalar(
            select(OwnerChatMessage.content).where(
                OwnerChatMessage.reply_to_message_id == pending.originating_message_id,
                OwnerChatMessage.conversation_id == owner.conversation_id,
                OwnerChatMessage.role == ChatMessageRole.ASSISTANT,
            )
        )
        return {
            "operation": "preference",
            "expected_reply": "selection",
            "preference_key": pending.preference_key,
            "owner_request": origin.content,
            "question": question,
            "candidates": [
                {"label": item.label, "location_type": item.location_type}
                for item in candidates
            ],
        }
    pending = _pending_product_clarification(
        session, executor, user, business_id, owner.id
    )
    if isinstance(pending, PendingCatalogueClarification):
        return {
            "operation": PRODUCT_SEARCH_TOOL,
            "expected_reply": "selection",
            "owner_request": pending.owner_request,
            "question": _pending_inventory_reply(pending),
            "candidates": [
                {"names": item.names, "sku": item.sku} for item in pending.candidates
            ],
        }
    previous_state = _previous_product_clarification(session, owner.id)
    if (
        pending is None
        and (previous_state or {}).get("operation") == PRODUCT_SEARCH_TOOL
    ):
        return {
            "operation": PRODUCT_SEARCH_TOOL,
            "expected_reply": "restart",
            "status": "invalid_or_expired",
        }
    previous_owner = session.scalar(
        select(OwnerChatMessage).where(
            OwnerChatMessage.conversation_id == owner.conversation_id,
            OwnerChatMessage.sequence_number == owner.sequence_number - 2,
            OwnerChatMessage.role == ChatMessageRole.OWNER,
            OwnerChatMessage.generation_state == ChatGenerationState.COMPLETED,
        )
    )
    previous_reply = session.scalar(
        select(OwnerChatMessage).where(
            OwnerChatMessage.conversation_id == owner.conversation_id,
            OwnerChatMessage.sequence_number == owner.sequence_number - 1,
            OwnerChatMessage.role == ChatMessageRole.ASSISTANT,
            OwnerChatMessage.reply_to_message_id == previous_owner.id
            if previous_owner
            else False,
        )
    )
    if previous_owner is None or previous_reply is None:
        return None
    if pending is not None:
        candidates = (
            pending.candidates
            or pending.category_candidates
            or pending.location_candidates
        )
        operation = (
            "inventory_product"
            if pending.candidates
            else "inventory_category"
            if pending.category_candidates
            else "inventory_list"
        )
        return {
            "operation": operation,
            "expected_reply": "selection",
            "owner_request": pending.owner_request or previous_owner.content,
            "question": previous_reply.content,
            "candidates": [
                {
                    "label": getattr(
                        candidate, "name", getattr(candidate, "label", "")
                    ),
                    "sku": getattr(candidate, "sku", None),
                }
                for candidate in candidates
            ],
        }
    source = executor._active_source(business_id)
    if _pending_sales_clarification(
        session, owner.id, utc_now(), source.updated_at if source else None
    ):
        return {
            "operation": "sales_summary",
            "expected_reply": "details",
            "owner_request": previous_owner.content,
            "question": previous_reply.content,
        }
    return None


def _source_candidate_selection(
    message: str, query: object, candidates: tuple[object, ...]
) -> object | None:
    """Validate a source reference in the current reply, not a model guess."""
    normalized = _normalized_classifier_text(message).strip(" ?.!,،")
    phrase = (
        _normalized_classifier_text(query).strip(" ?.!,،")
        if isinstance(query, str)
        else ""
    )
    if not phrase:
        phrase = normalized
    grounded_phrase = bool(phrase and f" {phrase} " in f" {normalized} ")
    matches = []
    for candidate in candidates:
        values = tuple(
            _normalized_classifier_text(value)
            for field in (
                "external_product_id",
                "sku",
                "barcode",
                "name",
                "external_category_id",
                "external_location_id",
                "label",
            )
            if isinstance(value := getattr(candidate, field, None), str)
        )
        if isinstance(candidate, CatalogueProduct):
            values += tuple(
                _normalized_classifier_text(name) for name in candidate.names
            )
        if any(f" {value} " in f" {normalized} " for value in values) or (
            grounded_phrase and any(f" {phrase} " in f" {value} " for value in values)
        ):
            matches.append(candidate)
    return matches[0] if len(matches) == 1 else None


def _pending_inventory_reply(
    pending: PendingInventoryClarification | PendingCatalogueClarification,
) -> str:
    if isinstance(pending, PendingCatalogueClarification):
        return _catalogue_reply(
            CatalogueResult(
                mapping_version=pending.mapping_version,
                status="ambiguous",
                items=pending.candidates,
                truncated=pending.truncated,
            )
        )
    if pending.candidates:
        labels = [
            f"{item.name} ({item.sku})" if item.sku else item.name
            for item in pending.candidates
        ]
        kind = "product"
    else:
        labels = [
            item.label
            for item in (pending.category_candidates or pending.location_candidates)
        ]
        kind = "category" if pending.category_candidates else "location"
    return (
        f"Which {kind} do you mean: "
        + "; ".join(_safe_operational_label(label, kind) for label in labels)
        + "?"
    )


def _inventory_literal_references(message: str) -> tuple[str | None, str | None]:
    """Remove only inventory request syntax, preserving the product reference."""
    text = " ".join(message.split()).strip(" ?.!")
    match = re.fullmatch(
        r"(?:show me|what (?:i|do i|do we) have|how many|"
        r"what is (?:the )?(?:quantity|stock|inventory) of)\s+(.+)",
        text,
        flags=re.IGNORECASE,
    )
    if match is None:
        if re.fullmatch(r"[\w.-]{1,80}", text):
            return text, None
        return None, None
    reference = re.sub(
        r"\s+(?:do we have(?: left| remaining)?|we have left)$",
        "",
        match[1],
        flags=re.IGNORECASE,
    )
    location = re.fullmatch(r"(.+?)\s+(?:in|at|from)\s+(.+)", reference, re.I)
    if location is not None:
        label = location[2]
        if re.fullmatch(r"(?:stock(?: now)?|the (?:other )?location)", label, re.I):
            return None, None
        return location[1], label
    return reference, None


def _selected_pending_product(
    message: str, pending: PendingInventoryClarification | None
) -> ProductResolutionCandidate | None:
    if pending is None:
        return None
    reference, _ = _inventory_literal_references(message)
    normalized = " ".join((reference or message).split()).strip().casefold()
    matches = tuple(
        candidate
        for candidate in pending.candidates
        if normalized
        in {
            value.casefold()
            for value in (
                candidate.external_product_id,
                candidate.sku,
                candidate.barcode,
                candidate.name,
                f"{candidate.name} ({candidate.sku})" if candidate.sku else None,
            )
            if value
        }
    )
    return matches[0] if len(matches) == 1 else None


def _store_product_clarification(
    session: Session,
    executor: OperationalToolExecutor,
    user: User,
    business_id: uuid.UUID,
    owner_message_id: uuid.UUID,
    arguments: dict[str, object],
    output: object,
) -> None:
    if (
        not isinstance(output, InventoryResult)
        or output.resolution is None
        or output.resolution.status != "ambiguous"
    ):
        return
    source = executor._active_source(business_id)
    owner_message = session.get(OwnerChatMessage, owner_message_id)
    if source is None or owner_message is None:
        return
    pending = PendingInventoryClarification(
        user_id=user.id,
        source_id=source.id,
        source_updated_at=source.updated_at,
        expires_at=utc_now() + PREFERENCE_PENDING_TTL,
        arguments=InventoryQuery.model_validate(arguments),
        candidates=output.resolution.candidates,
        owner_request=owner_message.content,
    )
    owner_message.operational_clarification = pending.model_dump(mode="json")


def _catalogue_label(item: CatalogueProduct) -> str:
    return (
        "/".join(_safe_operational_label(name, "Product") for name in item.names)
        + f" (ID: {_safe_operational_label(item.external_product_id, 'product')})"
        + (f"; SKU: {_safe_operational_label(item.sku, 'unknown')}" if item.sku else "")
        + (
            "; category: "
            + "/".join(
                _safe_operational_label(label, "unknown") for label in item.categories
            )
            if item.categories
            else ""
        )
    )


def _chat_catalogue_result(output: CatalogueResult) -> CatalogueResult:
    """Only displayed choices are offered; leave space for the bounded reply."""
    size = 0
    offered = []
    for item in output.items:
        size += len(_catalogue_label(item)) + 2
        if size > 12_000:
            break
        offered.append(item)
    return output.model_copy(
        update={
            "items": tuple(offered),
            "truncated": output.truncated or len(offered) < len(output.items),
        }
    )


def _catalogue_reply(output: CatalogueResult) -> str:
    """Render only validated catalogue fields; quantities and prices are unknown."""
    if output.status == "not_found":
        return "No matching catalogue product was found. Stock and prices are unknown."
    output = _chat_catalogue_result(output)
    labels = [_catalogue_label(item) for item in output.items]
    reply = (
        "Which catalogue product do you mean: " + "; ".join(labels) + "?"
        if output.status == "ambiguous"
        else "Catalogue product: " + labels[0] + "."
    )
    if output.truncated:
        reply += " More variants may exist; narrow the search if yours is not offered."
    return reply + " Stock and prices are unknown."


def _store_catalogue_clarification(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    owner_message_id: uuid.UUID,
    executed: OperationalToolResult,
) -> None:
    output = executed.output
    if not isinstance(output, CatalogueResult) or output.status != "ambiguous":
        return
    output = _chat_catalogue_result(output)
    owner = session.get(OwnerChatMessage, owner_message_id)
    pending = PendingCatalogueClarification(
        business_id=business_id,
        user_id=user.id,
        conversation_id=owner.conversation_id,
        source_id=executed.source_id,
        source_updated_at=executed.source_updated_at,
        mapping_version=output.mapping_version,
        schema_fingerprint=executed.schema_fingerprint,
        source_fingerprint=executed.source_fingerprint,
        expires_at=utc_now() + PREFERENCE_PENDING_TTL,
        candidates=output.items,
        truncated=output.truncated,
        owner_request=owner.content,
    )
    owner.operational_clarification = pending.model_dump(mode="json")


def _store_inventory_choice(
    session: Session,
    executor: OperationalToolExecutor,
    user: User,
    business_id: uuid.UUID,
    owner_message_id: uuid.UUID,
    arguments: dict[str, object],
    *,
    category_candidates: tuple[CategoryCandidate, ...] = (),
    location_candidates: tuple[LocationCandidate, ...] = (),
) -> None:
    source = executor._active_source(business_id)
    owner = session.get(OwnerChatMessage, owner_message_id)
    if source is None or owner is None:
        return
    proposal = dict(arguments)
    location_reference = proposal.pop("location_reference", None)
    pending = PendingInventoryClarification(
        user_id=user.id,
        source_id=source.id,
        source_updated_at=source.updated_at,
        expires_at=utc_now() + PREFERENCE_PENDING_TTL,
        arguments=InventoryQuery.model_validate(proposal),
        category_candidates=category_candidates[:5],
        location_candidates=location_candidates[:5],
        location_reference=location_reference,
        owner_request=owner.content,
    )
    owner.operational_clarification = pending.model_dump(mode="json")


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
    selected_sources: list[ProviderSource] | None = None,
    budget: _OperationalUsageBudget | None = None,
) -> tuple[OwnerChatResult, TokenUsage]:
    tool_results: list[ProviderToolResult] = []
    aggregate_usage: TokenUsage | None = None
    requested_at = utc_now()
    try:
        reporting_timezone, supported_metrics = executor.sales_reporting_context(
            user, business_id
        )
    except ToolExecutionError:
        business = _load_provider_business(session, business_id)
        reporting_timezone, supported_metrics = business.timezone, ()
    source = executor._active_source(business_id)
    pending_sales = _pending_sales_clarification(
        session,
        claim.message_id,
        requested_at,
        source.updated_at if source is not None else None,
    )
    pending_product = _pending_product_clarification(
        session, executor, user, business_id, claim.message_id
    )
    pending_catalogue = (
        pending_product
        if isinstance(pending_product, PendingCatalogueClarification)
        else None
    )
    pending_product = (
        pending_product
        if isinstance(pending_product, PendingInventoryClarification)
        else None
    )
    pending_product_candidates = (
        tuple(
            ProviderProductCandidate(label=candidate.name, sku=candidate.sku)
            for candidate in pending_product.candidates
        )
        if pending_product is not None and pending_product.candidates
        else ()
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
            user=user,
            executor=executor,
        )
        request = prepared.request
        request = replace(
            request,
            reporting_timezone=reporting_timezone,
            pending_sales_clarification=pending_sales,
        )
        try:
            result = _validated_generation(provider, request, budget)
        except OwnerChatProviderError as exc:
            _logger.info(
                "owner_chat_operational_plan validation=rejected reason=%s",
                exc.reason,
            )
            if (
                isinstance(exc, OwnerChatProviderInvalidResponse)
                and exc.usage is not None
                and exc.usage.authoritative
                and (request.pending_clarification or {}).get("operation")
                == "preference"
            ):
                # Discard the rejected plan; preserve a source-validated question.
                # Unknown usage still follows the existing failed-turn charge path.
                recovered_usage = _add_usage(aggregate_usage, exc.usage)
                recovery = _complete_pending_preference_if_selected(
                    session,
                    business_id,
                    claim,
                    user,
                    provider,
                    request,
                    OwnerChatResult(
                        pending_reply="unresolved",
                        provider_identifier=exc.provider_identifier,
                        model_identifier=exc.model_identifier,
                    ),
                    recovered_usage,
                    executor,
                )
                if recovery is not None and recovery[0] is not None:
                    return recovery
            if (
                isinstance(exc, OwnerChatProviderInvalidResponse)
                and exc.reason
                in {
                    "schema_validation_failed",
                    "output_truncated",
                    "planner_missing_pending_reply",
                }
                and exc.usage is not None
                and exc.usage.authoritative
                and (pending_product is not None or pending_catalogue is not None)
            ):
                # The rejected plan cannot execute. Known returned usage is still
                # charged, while the source-backed question remains answerable.
                aggregate_usage = _add_usage(aggregate_usage, exc.usage)
                owner = session.get(OwnerChatMessage, claim.message_id)
                pending = pending_product or pending_catalogue
                owner.operational_clarification = pending.model_dump(mode="json")
                session.commit()
                return OwnerChatResult(
                    reply=_pending_inventory_reply(pending),
                    usage=aggregate_usage,
                    provider_identifier=exc.provider_identifier,
                    model_identifier=exc.model_identifier,
                ), aggregate_usage
            if aggregate_usage is not None and not exc.usage_uncertain:
                exc.usage = (
                    _add_usage(aggregate_usage, exc.usage)
                    if exc.usage is not None
                    else aggregate_usage
                )
            raise
        catalogue_arguments = None
        if (request.pending_clarification or {}).get(
            "operation"
        ) == PRODUCT_SEARCH_TOOL:
            selection = (
                _source_candidate_selection(
                    request.messages[-1].content,
                    (result.tool_arguments or {}).get("query"),
                    pending_catalogue.candidates,
                )
                if pending_catalogue is not None
                and result.pending_reply not in {"unrelated", "cancel"}
                else None
            )
            if result.pending_reply == "cancel" or (
                result.pending_reply != "unrelated" and selection is None
            ):
                aggregate_usage = _add_usage(
                    aggregate_usage, _usage_for_result(provider, request, result)
                )
                owner = session.get(OwnerChatMessage, claim.message_id)
                if result.pending_reply == "cancel":
                    reply = "The pending catalogue choice is cancelled."
                elif pending_catalogue is None:
                    reply = (
                        "That catalogue selection is expired or no longer valid. "
                        "Start a new search."
                    )
                else:
                    owner.operational_clarification = pending_catalogue.model_dump(
                        mode="json"
                    )
                    reply = _pending_inventory_reply(pending_catalogue)
                session.commit()
                return OwnerChatResult(
                    reply=reply,
                    usage=aggregate_usage,
                    provider_identifier=result.provider_identifier,
                    model_identifier=result.model_identifier,
                ), aggregate_usage
            if selection is not None:
                catalogue_arguments = CatalogueRequest(
                    external_product_id=selection.external_product_id,
                    mapping_version=pending_catalogue.mapping_version,
                ).model_dump(mode="json", exclude_none=True)
        original_action = result.decision
        selected_product = (
            _selected_pending_product(request.messages[-1].content, pending_product)
            if result.pending_reply != "unrelated"
            else None
        )
        pending_candidates = (
            pending_product.candidates
            or pending_product.category_candidates
            or pending_product.location_candidates
            if pending_product is not None
            else ()
        )
        selection_query = (
            result.entity_query
            if pending_product is not None and not pending_product.location_candidates
            else (result.tool_arguments or {}).get("location_reference")
        )
        selected_choice = (
            _source_candidate_selection(
                request.messages[-1].content, selection_query, pending_candidates
            )
            if pending_candidates and result.pending_reply != "unrelated"
            else None
        )
        if isinstance(selected_choice, ProductResolutionCandidate):
            selected_product = selected_choice
        if selected_choice is not None and (
            (result.tool_name is not None and result.tool_name not in executor.registry)
            or (
                result.tool_arguments is not None
                and _contains_control_payload(result.tool_arguments)
            )
        ):
            raise OwnerChatProviderInvalidResponse(
                reason="prohibited_provider_arguments",
                usage=_add_usage(
                    aggregate_usage, _usage_for_result(provider, request, result)
                ),
                provider_identifier=result.provider_identifier,
                model_identifier=result.model_identifier,
            )
        if selected_product is not None:
            if (
                result.tool_name is not None
                and result.tool_name not in executor.registry
            ) or (
                result.tool_arguments is not None
                and _contains_control_payload(result.tool_arguments)
            ):
                raise OwnerChatProviderInvalidResponse(
                    reason="prohibited_provider_arguments",
                    usage=_add_usage(
                        aggregate_usage, _usage_for_result(provider, request, result)
                    ),
                    provider_identifier=result.provider_identifier,
                    model_identifier=result.model_identifier,
                )
            result = replace(
                result,
                decision="tool",
                semantic_operation="inventory_product",
                entity_kind="product",
                entity_query=selected_product.external_product_id,
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments=None,
                preference_key=None,
                location_reference=None,
            )
        # Only the dedicated resolver may supply an executable category reference.
        # A planner-provided reference is untrusted until it is revalidated against
        # the exact bounded candidates in a separate compact request.
        result = replace(result, category_candidate_reference=None)
        result, command = _consistent_operational_plan(
            result, executor, request.category_candidates, request
        )
        if catalogue_arguments is not None:
            command = _ValidatedOperationalCommand(
                tool_name=PRODUCT_SEARCH_TOOL,
                arguments=catalogue_arguments,
                provider_tool_fields="backend_selection",
                consistency_outcome="accepted",
            )
            result = replace(
                result,
                decision="tool",
                semantic_operation=PRODUCT_SEARCH_TOOL,
                tool_name=PRODUCT_SEARCH_TOOL,
                tool_arguments=catalogue_arguments,
                entity_kind=None,
                entity_query=None,
                reply="",
                preference_key=None,
                location_reference=None,
            )
        _logger.info(
            "owner_chat_operational_plan semantic_operation=%s entity_kind=%s "
            "original_action=%s effective_action=%s consistency_outcome=%s "
            "plan_contract=interpreted provider_tool_fields=%s "
            "tool_derivation=semantic_registry effective_tool=%s metric=%s "
            "execution_validation=%s",
            result.semantic_operation,
            result.entity_kind,
            original_action,
            result.decision,
            command.consistency_outcome if command is not None else "accepted",
            command.provider_tool_fields if command is not None else "missing",
            result.tool_name if result.decision == "tool" else None,
            _planned_metric(result.tool_arguments),
            "accepted" if command is not None else "rejected",
        )
        call_usage = _usage_for_result(provider, request, result)
        aggregate_usage = _add_usage(aggregate_usage, call_usage)
        if budget is not None:
            budget.usage = aggregate_usage
        if call_usage.output_tokens > request.max_output_tokens:
            raise OwnerChatProviderInvalidResponse(
                usage=aggregate_usage,
                provider_identifier=result.provider_identifier,
                model_identifier=result.model_identifier,
            )
        if (
            source is not None
            and source.mapping_profile_key == "discovered_products"
            and result.semantic_operation
            not in {PRODUCT_SEARCH_TOOL, "conversation", "knowledge"}
        ):
            return OwnerChatResult(
                reply="The connected catalogue provides product details only. "
                "Stock, prices, inventory and sales are unknown.",
                usage=aggregate_usage,
                provider_identifier=result.provider_identifier,
                model_identifier=result.model_identifier,
            ), aggregate_usage
        if (
            source is not None
            and source.mapping_profile_key == "discovered_products"
            and result.semantic_operation == PRODUCT_SEARCH_TOOL
            and command is None
        ):
            return OwnerChatResult(
                reply="Specify a product name or identifier to search the catalogue. "
                "Stock and prices are unknown.",
                usage=aggregate_usage,
                provider_identifier=result.provider_identifier,
                model_identifier=result.model_identifier,
            ), aggregate_usage
        if (
            pending_product is not None
            and selected_product is None
            and (
                selected_choice is not None
                or result.pending_reply in {"selection", "confirmation", "unresolved"}
                or (
                    result.pending_reply is None
                    and result.semantic_operation
                    == (request.pending_clarification or {}).get("operation")
                )
            )
        ):
            if selected_choice is None:
                owner = session.get(OwnerChatMessage, claim.message_id)
                owner.operational_clarification = pending_product.model_dump(
                    mode="json"
                )
                session.commit()
                return OwnerChatResult(
                    reply=_pending_inventory_reply(pending_product),
                    usage=aggregate_usage,
                    provider_identifier=result.provider_identifier,
                    model_identifier=result.model_identifier,
                ), aggregate_usage
            arguments = pending_product.arguments.model_dump(exclude_none=True)
            if pending_product.location_reference is not None:
                arguments["location_reference"] = pending_product.location_reference
            if pending_product.category_candidates:
                arguments["category_filter"] = selected_choice.label
                result = replace(
                    result,
                    decision="tool",
                    semantic_operation="inventory_category",
                    entity_kind="category",
                    entity_query=selected_choice.label,
                    tool_name=CURRENT_INVENTORY_TOOL,
                    tool_arguments=arguments,
                    reply="",
                    preference_key=None,
                    location_reference=None,
                )
            else:
                arguments.pop("branch_external_id", None)
                arguments.pop("warehouse_external_id", None)
                arguments["location_reference"] = selected_choice.external_location_id
                result = replace(
                    result,
                    decision="tool",
                    semantic_operation="inventory_list",
                    entity_kind=None,
                    entity_query=None,
                    tool_name=CURRENT_INVENTORY_TOOL,
                    tool_arguments=arguments,
                    reply="",
                    preference_key=None,
                    location_reference=None,
                )
            result, command = _consistent_operational_plan(
                result, executor, request.category_candidates, request
            )

        if (
            result.semantic_operation in {"sales_summary", "best_selling_products"}
            and command is None
        ):
            _logger.info(
                "owner_chat_operational_dispatch outcome=clarification "
                "tool_derivation=semantic_registry effective_tool=%s "
                "execution_validation=rejected",
                SALES_SUMMARY_TOOL,
            )
            return (
                OwnerChatResult(
                    reply=(
                        _SALES_CLARIFICATION_REPLY
                        + " Available metrics: "
                        + ", ".join(
                            metric.replace("_", " ") for metric in supported_metrics
                        )
                        + ". Last month means the previous completed calendar month."
                    ),
                    usage=aggregate_usage,
                    provider_identifier=result.provider_identifier,
                    model_identifier=result.model_identifier,
                    decision="final",
                ),
                aggregate_usage,
            )

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
                result, redispatch_command = _consistent_operational_plan(
                    result, executor, request.category_candidates
                )
                _logger.info(
                    "owner_chat_category_resolution outcome=exact redispatch=%s",
                    (
                        redispatch_command.consistency_outcome
                        if redispatch_command is not None
                        else "rejected"
                    ),
                )
            else:
                category_request = _build_category_resolution_request(
                    request, result.entity_query
                )
                try:
                    category_result = _validated_generation(
                        provider, category_request, budget
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
                            result, redispatch_command = _consistent_operational_plan(
                                result, executor, request.category_candidates
                            )
                            _logger.info(
                                "owner_chat_category_resolution outcome=matched "
                                "redispatch=%s",
                                (
                                    redispatch_command.consistency_outcome
                                    if redispatch_command is not None
                                    else "rejected"
                                ),
                            )
                        elif category_result.category_resolution_status == "ambiguous":
                            references = set(
                                category_result.category_candidate_references
                            )
                            _store_inventory_choice(
                                session,
                                executor,
                                user,
                                business_id,
                                claim.message_id,
                                result.tool_arguments or {},
                                category_candidates=tuple(
                                    CategoryCandidate(
                                        external_category_id=item.external_category_id,
                                        label=item.label,
                                    )
                                    for item in request.category_candidates
                                    if item.external_category_id in references
                                ),
                            )
                            session.commit()
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

        pending_completion = _complete_pending_preference_if_selected(
            session,
            business_id,
            claim,
            user,
            provider,
            request,
            result,
            aggregate_usage,
            executor,
        )
        if pending_completion is not None:
            completed_preference, aggregate_usage = pending_completion
            if completed_preference is not None:
                return completed_preference, aggregate_usage

        if result.decision == "tool":
            owner = session.get(OwnerChatMessage, claim.message_id)
            source = executor._active_source(business_id)
            if owner is not None and source is not None:
                _supersede_pending_preference(
                    session, user, business_id, owner.conversation_id, source.id
                )
                session.commit()

        if result.semantic_operation == "product_price":
            return (
                OwnerChatResult(
                    reply="The connected source does not provide "
                    "current product prices. "
                    "Historical sale prices cannot establish a current price.",
                    usage=aggregate_usage,
                    provider_identifier=result.provider_identifier,
                    model_identifier=result.model_identifier,
                ),
                aggregate_usage,
            )

        if result.semantic_operation in {
            "conversation",
            "knowledge",
        } and _is_live_operational_request(request.messages[-1].content):
            return _operational_synthesis_fallback(
                {"capability": "operational_planning", "source_connected": True},
                aggregate_usage,
                result.provider_identifier,
                result.model_identifier,
            ), aggregate_usage

        if result.semantic_operation in {
            "conversation",
            "knowledge",
        } and not _is_live_operational_request(request.messages[-1].content):
            owner = session.get(OwnerChatMessage, claim.message_id)
            source = executor._active_source(business_id)
            if owner is not None and source is not None:
                _supersede_pending_preference(
                    session, user, business_id, owner.conversation_id, source.id
                )
                session.commit()
            routed = (
                _build_conversation_request(
                    session, business_id, claim.message_id, settings
                )
                if result.semantic_operation == "conversation"
                else _build_provider_request(
                    session, user, business_id, claim.message_id, settings
                )
            )
            if not routed.has_usable_evidence:
                reply = _missing_knowledge_reply(
                    request.messages[-1].content, prepared.business.default_language
                )
                routed_result = OwnerChatResult(reply=reply)
            else:
                try:
                    routed_result = _validated_generation(
                        provider, routed.request, budget
                    )
                except OwnerChatProviderError as exc:
                    exc.usage = _add_usage(
                        aggregate_usage,
                        _provider_failure_usage(provider, routed.request, exc),
                    )
                    raise
                routed_usage = _usage_for_result(
                    provider, routed.request, routed_result
                )
                aggregate_usage = _add_usage(aggregate_usage, routed_usage)
                if routed_usage.output_tokens > routed.request.max_output_tokens:
                    raise OwnerChatProviderInvalidResponse(usage=aggregate_usage)
                if (
                    routed.request.mode == "conversation"
                    and routed_result.requires_business_knowledge
                ):
                    routed_result = replace(
                        routed_result,
                        reply=_missing_knowledge_reply(
                            request.messages[-1].content,
                            prepared.business.default_language,
                        ),
                    )
                conflict_labels = _conflicting_source_labels(routed.request.sources)
                if conflict_labels:
                    routed_result = _enforce_conflict_result(
                        routed_result,
                        request.messages[-1].content,
                        prepared.business.default_language,
                        routed.request.sources,
                        conflict_labels,
                    )
            if selected_sources is not None:
                selected_sources.extend(routed.request.sources)
            return replace(
                routed_result,
                usage=aggregate_usage,
                semantic_operation=result.semantic_operation,
            ), aggregate_usage

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
            return _run_preference_intent(
                session,
                business_id,
                claim,
                user,
                provider,
                request,
                result,
                aggregate_usage,
                executor,
                budget,
            )
            intent = _interpret_preference_intent(result)
            source_candidates = tuple(executor.location_candidates(user, business_id))
            preference_plan = (
                "complete"
                if intent.preference_key is not None
                and (
                    intent.action == "clear_preference"
                    or intent.location_reference is not None
                )
                else "incomplete"
            )
            preference_resolution = "deterministic"
            resolver_invoked = False
            command = _validated_preference_command(
                intent,
                _resolved_preference_key(intent),
                _deterministic_preference_location(
                    intent.location_reference, source_candidates
                ),
            )
            if (
                intent.action == "set_preference"
                and command is None
                and preference_plan == "incomplete"
            ):
                resolver_invoked = True
                resolver_candidates = _preference_location_candidates(source_candidates)
                resolver_request = _build_preference_resolution_request(
                    request, resolver_candidates
                )
                try:
                    resolver_result = _validated_generation(
                        provider, resolver_request, budget
                    )
                except OwnerChatProviderInvalidResponse as exc:
                    failure_usage = exc.usage
                    if failure_usage is None and exc.usage_uncertain:
                        estimated_input = provider.estimate_input_tokens(
                            resolver_request
                        )
                        failure_usage = TokenUsage(
                            input_tokens=estimated_input,
                            output_tokens=resolver_request.max_output_tokens,
                            total_tokens=(
                                estimated_input + resolver_request.max_output_tokens
                            ),
                            authoritative=False,
                        )
                    if failure_usage is not None:
                        aggregate_usage = _add_usage(aggregate_usage, failure_usage)
                    _logger.info(
                        "owner_chat_preference preference_plan=%s "
                        "preference_resolution=invalid resolver_invoked=true "
                        "preference_persisted=false",
                        preference_plan,
                    )
                    return (
                        OwnerChatResult(
                            reply=_preference_resolution_reply("invalid", (), ()),
                            usage=aggregate_usage,
                            provider_identifier=result.provider_identifier,
                            model_identifier=result.model_identifier,
                        ),
                        aggregate_usage,
                    )
                except OwnerChatProviderError as exc:
                    failure_usage = exc.usage
                    if failure_usage is None and exc.usage_uncertain:
                        estimated_input = provider.estimate_input_tokens(
                            resolver_request
                        )
                        failure_usage = TokenUsage(
                            input_tokens=estimated_input,
                            output_tokens=resolver_request.max_output_tokens,
                            total_tokens=(
                                estimated_input + resolver_request.max_output_tokens
                            ),
                            authoritative=False,
                        )
                    if failure_usage is not None:
                        aggregate_usage = _add_usage(aggregate_usage, failure_usage)
                    _logger.info(
                        "owner_chat_preference preference_plan=%s "
                        "preference_resolution=provider_failure resolver_invoked=true "
                        "preference_persisted=false",
                        preference_plan,
                    )
                    return (
                        OwnerChatResult(
                            reply=_preference_resolution_reply(
                                "provider_failure", (), ()
                            ),
                            usage=aggregate_usage,
                            provider_identifier=result.provider_identifier,
                            model_identifier=result.model_identifier,
                        ),
                        aggregate_usage,
                    )
                resolver_usage = _usage_for_result(
                    provider, resolver_request, resolver_result
                )
                aggregate_usage = _add_usage(aggregate_usage, resolver_usage)
                preference_resolution = (
                    resolver_result.preference_resolution_status or "invalid"
                )
                candidate_by_reference = {
                    provider_candidate.reference: source_candidate
                    for provider_candidate, source_candidate in zip(
                        resolver_candidates, source_candidates, strict=True
                    )
                }
                if preference_resolution == "matched":
                    location_reference = (
                        resolver_result.preference_location_candidate_references[0]
                    )
                    command = _validated_preference_command(
                        intent,
                        resolver_result.preference_resolution_key,
                        candidate_by_reference.get(location_reference),
                    )
                    if command is None:
                        preference_resolution = "invalid"
                if command is None:
                    _logger.info(
                        "owner_chat_preference preference_plan=%s "
                        "preference_resolution=%s "
                        "resolver_invoked=true preference_persisted=false",
                        preference_plan,
                        preference_resolution,
                    )
                    return (
                        OwnerChatResult(
                            reply=_preference_resolution_reply(
                                preference_resolution,
                                resolver_result.preference_location_candidate_references,
                                resolver_candidates,
                            ),
                            usage=aggregate_usage,
                            provider_identifier=result.provider_identifier,
                            model_identifier=result.model_identifier,
                        ),
                        aggregate_usage,
                    )
            if command is None:
                matches = tuple(
                    candidate
                    for candidate in source_candidates
                    if isinstance(intent.location_reference, str)
                    and " ".join(intent.location_reference.split()).casefold()
                    in " ".join(candidate.label.split()).casefold()
                )
                preference_result = ProviderToolResult(
                    tool_name="inventory_location_preference",
                    output={
                        "action": "not_saved",
                        "capability": "inventory_location_preference",
                        "resolution": {
                            "status": "ambiguous" if matches else "not_found",
                            "candidates": [
                                {
                                    "label": candidate.label,
                                    "location_type": candidate.location_type,
                                }
                                for candidate in matches
                            ],
                        },
                    },
                )
                _logger.info(
                    "owner_chat_preference preference_plan=%s preference_resolution=%s "
                    "resolver_invoked=%s preference_persisted=false",
                    preference_plan,
                    preference_resolution,
                    str(resolver_invoked).lower(),
                )
            else:
                try:
                    preference_result = _save_inventory_location_preference(
                        session,
                        executor,
                        user,
                        business_id,
                        command,
                    )
                except ToolExecutionError:
                    _logger.info(
                        "owner_chat_preference preference_plan=%s "
                        "preference_resolution=%s "
                        "resolver_invoked=%s preference_persisted=false",
                        preference_plan,
                        preference_resolution,
                        str(resolver_invoked).lower(),
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
                _logger.info(
                    "owner_chat_preference preference_plan=%s preference_resolution=%s "
                    "resolver_invoked=%s preference_persisted=true",
                    preference_plan,
                    preference_resolution,
                    str(resolver_invoked).lower(),
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
                synthesis = _validated_generation(provider, synthesis_request, budget)
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
            literal_product, literal_location = _inventory_literal_references(
                request.messages[-1].content
            )
            if result.semantic_operation != "inventory_product":
                literal_product, literal_location = None, None
            if selected_product is not None and pending_product is not None:
                try:
                    resolution = executor.resolve_product(
                        user, business_id, selected_product.external_product_id
                    )
                except ToolExecutionError:
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
                if (
                    resolution.status != "resolved"
                    or resolution.product is None
                    or resolution.product.external_product_id
                    != selected_product.external_product_id
                ):
                    return (
                        OwnerChatResult(
                            reply="That selection is no longer a unique live product. "
                            "Please make a new inventory request.",
                            usage=aggregate_usage,
                            provider_identifier=result.provider_identifier,
                            model_identifier=result.model_identifier,
                        ),
                        aggregate_usage,
                    )
                arguments = pending_product.arguments.model_dump(exclude_none=True)
                arguments["product_filter"] = selected_product.external_product_id
            elif literal_product and result.semantic_operation == "inventory_product":
                # Prefer the owner's complete reference to a shortened model query.
                if len(literal_product) <= 80:
                    arguments = {**arguments, "product_filter": literal_product}
            location_phrase = re.search(
                r"\b(?:in|at|from)\s+(.+?)[?.!]*$",
                request.messages[-1].content,
                re.I,
            )
            literal_locations = (
                _preference_locations_in_message(
                    literal_location or location_phrase[1],
                    tuple(executor.location_candidates(user, business_id)),
                )
                if literal_location or location_phrase is not None
                else ()
            )
            if len(literal_locations) == 1:
                literal_location = literal_locations[0].external_location_id
            if literal_location:
                arguments = dict(arguments)
                arguments.pop("branch_external_id", None)
                arguments.pop("warehouse_external_id", None)
                arguments["location_reference"] = literal_location
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
                    session,
                    executor,
                    user,
                    business_id,
                    arguments,
                    owner_message=request.messages[-1].content,
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
                output = location_preparation.result.output
                if isinstance(output, dict) and output.get("status") == "ambiguous":
                    offered = {
                        (item["label"], item["location_type"])
                        for item in output["candidates"]
                    }
                    _store_inventory_choice(
                        session,
                        executor,
                        user,
                        business_id,
                        claim.message_id,
                        location_preparation.arguments,
                        location_candidates=tuple(
                            item
                            for item in executor.location_candidates(user, business_id)
                            if (item.label, item.location_type) in offered
                        ),
                    )
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
                    synthesis = _validated_generation(
                        provider, synthesis_request, budget
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
                except OwnerChatProviderError as exc:
                    aggregate_usage = _add_usage(
                        aggregate_usage,
                        _provider_failure_usage(provider, synthesis_request, exc),
                    )
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
        if isinstance(executed.output, CatalogueResult):
            _store_catalogue_clarification(
                session, user, business_id, claim.message_id, executed
            )
            return OwnerChatResult(
                reply=_catalogue_reply(executed.output),
                usage=aggregate_usage,
                provider_identifier=result.provider_identifier,
                model_identifier=result.model_identifier,
            ), aggregate_usage
        if executed.tool_name == CURRENT_INVENTORY_TOOL:
            _store_product_clarification(
                session,
                executor,
                user,
                business_id,
                claim.message_id,
                arguments,
                executed.output,
            )
            if (
                isinstance(executed.output, InventoryResult)
                and executed.output.category_resolution is not None
                and executed.output.category_resolution.status == "ambiguous"
            ):
                _store_inventory_choice(
                    session,
                    executor,
                    user,
                    business_id,
                    claim.message_id,
                    arguments,
                    category_candidates=executed.output.category_resolution.candidates,
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
            synthesis = _validated_generation(provider, synthesis_request, budget)
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
        aggregate_usage = _add_usage(aggregate_usage, synthesis_usage)
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
        _logger.info(
            "owner_chat_operational_synthesis outcome=final schema=response_only"
        )
        return (
            _operational_result_with_usage(synthesis, aggregate_usage),
            aggregate_usage,
        )

    raise RuntimeError("Operational provider planner exited unexpectedly.")


def _validate_pending_preference_reply(
    result: OwnerChatResult, request: OwnerChatRequest
) -> None:
    """Require AI-classified, current-message evidence before abandoning a choice."""
    if (request.pending_clarification or {}).get("operation") == PRODUCT_SEARCH_TOOL:
        if result.pending_reply == "replace":
            raise OwnerChatProviderInvalidResponse(reason="invalid_pending_transition")
        if result.pending_reply in {"cancel", "unrelated"}:
            quote = result.pending_request
            message = " ".join(request.messages[-1].content.split())
            if (
                not isinstance(quote, str)
                or not quote.strip()
                or " ".join(quote.split()) not in message
            ):
                raise OwnerChatProviderInvalidResponse(
                    reason="invalid_pending_transition"
                )
        elif result.pending_request is not None:
            raise OwnerChatProviderInvalidResponse(reason="invalid_pending_transition")
        return
    if (request.pending_clarification or {}).get("operation") != "preference":
        if result.pending_reply in {"cancel", "replace"} or result.pending_request:
            raise OwnerChatProviderInvalidResponse(reason="invalid_pending_transition")
        return
    if result.pending_reply not in {
        "selection",
        "confirmation",
        "unresolved",
        "unrelated",
        "cancel",
        "replace",
    }:
        raise OwnerChatProviderInvalidResponse(reason="invalid_pending_transition")
    if result.pending_reply in {"cancel", "replace", "unrelated"}:
        quote = result.pending_request
        message = " ".join(request.messages[-1].content.split())
        if (
            not isinstance(quote, str)
            or not 1 <= len(quote.strip()) <= 255
            or " ".join(quote.split()) not in message
            or (
                result.pending_reply == "replace"
                and result.decision not in {"set_preference", "clear_preference"}
            )
            or (
                result.pending_reply == "cancel"
                and (
                    result.decision != "final"
                    or result.semantic_operation != "conversation"
                )
            )
            or (
                result.pending_reply == "unrelated"
                and result.decision in {"set_preference", "clear_preference"}
            )
        ):
            raise OwnerChatProviderInvalidResponse(reason="invalid_pending_transition")
    elif result.pending_request is not None or result.decision == "clear_preference":
        raise OwnerChatProviderInvalidResponse(reason="invalid_pending_transition")


def _validate_result(result: object, request: OwnerChatRequest) -> OwnerChatResult:
    if not isinstance(result, OwnerChatResult):
        raise OwnerChatProviderInvalidResponse
    if request.mode == "operational":
        if result.pending_reply not in {
            None,
            "selection",
            "confirmation",
            "unresolved",
            "unrelated",
            "cancel",
            "replace",
        }:
            raise OwnerChatProviderInvalidResponse(reason="invalid_pending_reply")
        _validate_pending_preference_reply(result, request)
        result = normalize_legacy_operational_preference(result)
        if result.semantic_operation not in {
            "product_search",
            "inventory_product",
            "inventory_category",
            "inventory_list",
            "restocking",
            "sales_summary",
            "best_selling_products",
            "preference",
            "knowledge",
            "conversation",
            "product_price",
            "unsupported",
        } or (
            result.semantic_operation in {"knowledge", "conversation", "product_price"}
            and result.decision not in {"final", "unavailable"}
        ):
            raise OwnerChatProviderInvalidResponse(
                reason="invalid_operational_semantics"
            )
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
        if result.semantic_operation == "preference" and result.decision not in {
            "set_preference",
            "clear_preference",
        }:
            raise OwnerChatProviderInvalidResponse(
                reason="invalid_operational_semantics"
            )
        if result.decision == "tool":
            if (
                result.reply
                or (
                    result.tool_name is not None
                    and not isinstance(result.tool_name, str)
                )
                or (
                    result.tool_arguments is not None
                    and not isinstance(result.tool_arguments, dict)
                )
            ):
                raise OwnerChatProviderInvalidResponse(
                    reason="invalid_operational_response"
                )
        elif result.decision == "set_preference":
            if (
                result.semantic_operation != "preference"
                or result.reply
                or result.tool_name is not None
                or result.tool_arguments is not None
            ):
                raise OwnerChatProviderInvalidResponse(
                    reason="invalid_operational_response"
                )
            _interpret_preference_intent(result)
        elif result.decision == "clear_preference":
            if (
                result.semantic_operation != "preference"
                or result.reply
                or result.tool_name is not None
                or result.tool_arguments is not None
            ):
                raise OwnerChatProviderInvalidResponse(
                    reason="invalid_operational_response"
                )
            _interpret_preference_intent(result)
        elif result.decision in {"final", "unavailable"}:
            delegated = result.decision == "final" and result.semantic_operation in {
                "conversation",
                "knowledge",
                "product_price",
            }
            if (
                not isinstance(result.reply, str)
                or (not delegated and not result.reply.strip())
                or len(result.reply.strip()) > 14_000
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
        if (
            request.validated_result_status == "data"
            and any(
                tool.tool_name == CURRENT_INVENTORY_TOOL
                and (
                    bool(tool.output.items)
                    if isinstance(tool.output, InventoryResult)
                    else isinstance(tool.output, dict)
                    and bool(tool.output.get("items"))
                )
                for tool in request.tool_results
            )
            and not any(character.isdecimal() for character in result.reply)
        ):
            # A quantity-free inventory acknowledgement is not a usable answer.
            # Existing synthesis failure handling renders the validated source data.
            raise OwnerChatProviderInvalidResponse(
                reason="missing_inventory_quantities"
            )
        for tool in request.tool_results:
            if (
                tool.tool_name == CURRENT_INVENTORY_TOOL
                and request.validated_result_status == "data"
            ):
                output = (
                    tool.output
                    if isinstance(tool.output, InventoryResult)
                    else InventoryResult.model_validate(tool.output)
                )
                # Free prose cannot establish row coverage or stock-unit conversions.
                # Render the bounded list from the same validated source result used
                # by failure fallback; retain the actual generation usage.
                return replace(
                    result,
                    reply=_operational_synthesis_fallback(
                        output,
                        result.usage or TokenUsage(0, 0, 0, authoritative=False),
                        result.provider_identifier,
                        result.model_identifier,
                    ).reply,
                )
            if tool.tool_name == "inventory_location":
                return replace(
                    result,
                    reply=_operational_synthesis_fallback(
                        tool.output,
                        result.usage or TokenUsage(0, 0, 0, authoritative=False),
                        result.provider_identifier,
                        result.model_identifier,
                    ).reply,
                )
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
    elif request.mode == "preference_resolution":
        candidate_references = {
            candidate.reference for candidate in request.preference_location_candidates
        }
        supported_keys = {
            capability.preference_key for capability in request.preference_capabilities
        }
        result_references = result.preference_location_candidate_references
        status = result.preference_resolution_status
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
            or result.preference_resolution_key not in supported_keys | {None}
            or not isinstance(result_references, tuple)
            or any(
                not isinstance(reference, str) or reference not in candidate_references
                for reference in result_references
            )
            or len(set(result_references)) != len(result_references)
            or (
                status == "matched"
                and (
                    result.preference_resolution_key is None
                    or len(result_references) != 1
                )
            )
            or (
                status == "ambiguous"
                and (
                    result.preference_resolution_key is not None
                    or len(result_references) < 2
                )
            )
            or (
                status == "no_match"
                and (result.preference_resolution_key is not None or result_references)
            )
        ):
            raise OwnerChatProviderInvalidResponse(
                reason="invalid_preference_resolution_response"
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
    budget: _OperationalUsageBudget | None = None
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
        has_pending_preference = (
            session.scalar(
                select(PendingOwnerOperationalPreference.id)
                .where(
                    PendingOwnerOperationalPreference.user_id == user.id,
                    PendingOwnerOperationalPreference.business_id == business_id,
                    PendingOwnerOperationalPreference.conversation_id
                    == owner_message.conversation_id,
                    PendingOwnerOperationalPreference.state == "pending",
                )
                .limit(1)
            )
            is not None
        )
        has_active_operational_source = False
        if profiles is not None and (
            has_pending_preference
            or not _is_general_conversation_request(owner_message.content)
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
            if not available and is_live_operational:
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
            budget = _OperationalUsageBudget(session, business, user, claim, settings)
            if executor is None:  # pragma: no cover - guarded above
                raise RuntimeError("Operational executor is unavailable.")
            selected_sources: list[ProviderSource] = []
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
                selected_sources=selected_sources,
                budget=budget,
            )
            _persist_result(
                session,
                business_id,
                claim,
                result,
                budget.reservation,
                aggregate_usage,
                tuple(selected_sources),
            )
            return result.semantic_operation not in {"conversation", "knowledge"}
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
                budget = _OperationalUsageBudget(
                    session, business, user, claim, settings
                )
                selected_sources = []
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
                    selected_sources=selected_sources,
                    budget=budget,
                )
                _persist_result(
                    session,
                    business_id,
                    claim,
                    result,
                    budget.reservation,
                    aggregate_usage,
                    tuple(selected_sources),
                )
                return result.semantic_operation not in {"conversation", "knowledge"}
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
        generation_attempt = _admit_provider_generation(
            session, business_id, claim, settings
        )
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
        if budget is not None:
            reservation = budget.reservation
            aggregate_usage = budget.usage
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
        if budget is not None and budget.calls > 1 and aggregate_usage is not None:
            outcome = "reported_failure"
        _mark_failed(
            session,
            claim,
            reservation=reservation,
            usage=aggregate_usage if budget is not None else exc.usage,
            outcome=outcome if reservation is not None else None,
            provider_identifier=exc.provider_identifier,
            model_identifier=exc.model_identifier,
        )
        raise _safe_provider_failure(exc) from None
    except ApplicationError:
        if budget is not None:
            reservation = budget.reservation
            aggregate_usage = budget.usage
        session.rollback()
        _mark_failed(
            session,
            claim,
            reservation=reservation,
            usage=aggregate_usage,
            outcome=(
                "reported_failure"
                if aggregate_usage is not None
                else "release"
                if reservation is not None
                else None
            ),
        )
        raise
    except ToolExecutionError:
        if budget is not None:
            reservation = budget.reservation
            aggregate_usage = budget.usage
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
        if budget is not None:
            reservation = budget.reservation
            aggregate_usage = budget.usage
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
