"""Persistent, ordered owner-chat orchestration."""

from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

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
    ProviderWorkingDay,
    ProviderWorkingShift,
)
from app.core.config import Settings
from app.core.exceptions import ApplicationError
from app.core.security import utc_now
from app.database.models import (
    Business,
    BusinessKnowledge,
    BusinessOpeningDay,
    ChatGenerationState,
    ChatMessageRole,
    OwnerChatMessage,
    OwnerConversation,
    User,
)
from app.schemas.owner_chat import (
    ChatMessageResponse,
    ConversationHistoryResponse,
    OwnerMessageRequest,
    OwnerTurnResponse,
)
from app.services.business_knowledge import upsert_proposed_knowledge
from app.services.business_profiles import is_business_profile_complete
from app.services.businesses import load_full_access_business

CHAT_CONTEXT_MESSAGE_LIMIT = 12
HISTORY_PAGE_SIZE = 50
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
    if not business.is_active or not is_business_profile_complete(business):
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
        .with_for_update()
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
    return ChatMessageResponse.model_validate(message, from_attributes=True)


def _completed_turn(
    session: Session, owner_message: OwnerChatMessage, replayed: bool
) -> OwnerTurnResponse | None:
    assistant = session.scalar(
        select(OwnerChatMessage).where(
            OwnerChatMessage.conversation_id == owner_message.conversation_id,
            OwnerChatMessage.reply_to_message_id == owner_message.id,
            OwnerChatMessage.role == ChatMessageRole.ASSISTANT,
        )
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
    settings: Settings,
) -> _Claim | None:
    session.scalar(
        select(OwnerConversation)
        .where(OwnerConversation.id == conversation_id)
        .with_for_update()
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
    oldest.generation_state = ChatGenerationState.PROCESSING
    oldest.generation_claim_token = token
    oldest.generation_claim_expires_at = now + timedelta(
        seconds=settings.owner_chat_generation_lease_seconds
    )
    oldest.generation_attempts += 1
    message_id = oldest.id
    session.commit()
    return _Claim(message_id=message_id, token=token)


def _select_relevant_knowledge(
    records: list[BusinessKnowledge], current_message: str
) -> tuple[ProviderKnowledge, ...]:
    terms = {term.casefold() for term in current_message.split() if len(term) >= 3}

    def rank(record: BusinessKnowledge) -> tuple[int, datetime, str]:
        searchable = f"{record.subject_key} {record.content}".casefold()
        matches = sum(term in searchable for term in terms)
        return matches, record.updated_at, str(record.id)

    ordered = sorted(records, key=rank, reverse=True)
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
    business_id: uuid.UUID,
    owner_message_id: uuid.UUID,
    settings: Settings,
) -> OwnerChatRequest:
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
    request = OwnerChatRequest(
        profile=ProviderBusinessProfile(
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
                        ProviderWorkingShift(
                            start=shift.opens_at,
                            end=shift.closes_at,
                        )
                        for shift in sorted(day.shifts, key=lambda item: item.opens_at)
                    ),
                )
                for day in sorted(
                    business.opening_days, key=lambda item: item.day_of_week
                )
            ),
        ),
        knowledge=_select_relevant_knowledge(knowledge, owner_message.content),
        messages=tuple(
            ProviderMessage(role=str(message.role), content=message.content)
            for message in messages
        ),
        requested_at=now,
    )
    session.commit()
    return request


def _mark_failed(session: Session, claim: _Claim) -> None:
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
    session.commit()


def _validate_result(result: object) -> OwnerChatResult:
    if not isinstance(result, OwnerChatResult):
        raise OwnerChatProviderInvalidResponse
    if (
        not isinstance(result.reply, str)
        or not 1 <= len(result.reply.strip()) <= 14_000
    ):
        raise OwnerChatProviderInvalidResponse
    if not isinstance(result.proposed_knowledge, tuple) or not all(
        hasattr(item, "subject_key")
        and hasattr(item, "content")
        and hasattr(item, "kind")
        and hasattr(item, "category")
        for item in result.proposed_knowledge
    ):
        raise OwnerChatProviderInvalidResponse
    return result


def _persist_result(
    session: Session,
    business_id: uuid.UUID,
    claim: _Claim,
    result: OwnerChatResult,
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
    session.commit()


def _generate_claimed_turn(
    session: Session,
    business_id: uuid.UUID,
    claim: _Claim,
    provider: OwnerChatProvider,
    settings: Settings,
) -> None:
    try:
        request = _build_provider_request(
            session, business_id, claim.message_id, settings
        )
        result = _validate_result(provider.generate(request))
    except OwnerChatProviderError:
        _mark_failed(session, claim)
        raise _provider_unavailable() from None
    except Exception:
        session.rollback()
        _mark_failed(session, claim)
        raise _provider_unavailable() from None
    try:
        _persist_result(session, business_id, claim, result)
    except Exception as exc:
        session.rollback()
        _mark_failed(session, claim)
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
        claim = _claim_oldest_turn(session, conversation.id, settings)
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
        _generate_claimed_turn(session, business_id, claim, provider, settings)
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
    query = select(OwnerChatMessage).where(
        OwnerChatMessage.conversation_id == conversation.id
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
