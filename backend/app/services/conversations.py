"""Tenant-scoped owner conversation lifecycle and pagination."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime

from fastapi import status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.exceptions import ApplicationError
from app.core.security import utc_now
from app.database.models import OwnerChatMessage, OwnerConversation, User
from app.schemas.owner_chat import ConversationListResponse, ConversationResponse
from app.services.businesses import load_full_access_business

CONVERSATION_PAGE_SIZE = 25
NEW_CONVERSATION_TITLE = "New conversation"


def _not_found() -> ApplicationError:
    return ApplicationError(
        "Conversation was not found.",
        status_code=status.HTTP_404_NOT_FOUND,
        error_code="conversation_not_found",
    )


def load_conversation(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> OwnerConversation:
    load_full_access_business(session, user, business_id)
    query = select(OwnerConversation).where(
        OwnerConversation.id == conversation_id,
        OwnerConversation.business_id == business_id,
    )
    if for_update:
        query = query.with_for_update()
    conversation = session.scalar(query)
    if conversation is None:
        raise _not_found()
    return conversation


def create_conversation(
    session: Session, user: User, business_id: uuid.UUID
) -> ConversationResponse:
    load_full_access_business(session, user, business_id)
    conversation = OwnerConversation(
        business_id=business_id,
        creator_user_id=user.id,
        channel="owner_web",
        title=NEW_CONVERSATION_TITLE,
    )
    session.add(conversation)
    session.commit()
    session.refresh(conversation)
    return conversation_response(session, conversation)


def get_default_conversation(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    *,
    create: bool,
) -> OwnerConversation | None:
    load_full_access_business(session, user, business_id)
    conversation = session.scalar(
        select(OwnerConversation)
        .where(
            OwnerConversation.business_id == business_id,
            OwnerConversation.archived.is_(False),
        )
        .order_by(
            OwnerConversation.last_message_at.desc().nullslast(),
            OwnerConversation.created_at.desc(),
            OwnerConversation.id.desc(),
        )
        .limit(1)
    )
    if conversation is None and create:
        created = create_conversation(session, user, business_id)
        conversation = session.get(OwnerConversation, created.id)
    return conversation


def get_conversation(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> ConversationResponse:
    return conversation_response(
        session, load_conversation(session, user, business_id, conversation_id)
    )


def archive_conversation(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> ConversationResponse:
    conversation = load_conversation(
        session, user, business_id, conversation_id, for_update=True
    )
    if not conversation.archived:
        conversation.archived = True
        conversation.archived_at = utc_now()
        session.commit()
        session.refresh(conversation)
    return conversation_response(session, conversation)


def _encode_cursor(conversation: OwnerConversation) -> str:
    activity = conversation.last_message_at or conversation.created_at
    payload = json.dumps(
        {"activity": activity.isoformat(), "id": str(conversation.id)},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        activity = datetime.fromisoformat(payload["activity"])
        if activity.utcoffset() is None:
            raise ValueError
        return activity, uuid.UUID(payload["id"])
    except ValueError, TypeError, KeyError, json.JSONDecodeError:
        raise ApplicationError(
            "Conversation cursor is invalid.",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            error_code="invalid_conversation_cursor",
        ) from None


def list_conversations(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    cursor: str | None,
    *,
    include_archived: bool = False,
) -> ConversationListResponse:
    load_full_access_business(session, user, business_id)
    activity = OwnerConversation.last_message_at
    query = select(OwnerConversation).where(
        OwnerConversation.business_id == business_id
    )
    if not include_archived:
        query = query.where(OwnerConversation.archived.is_(False))
    if cursor is not None:
        cursor_activity, cursor_id = _decode_cursor(cursor)
        effective_activity = func.coalesce(activity, OwnerConversation.created_at)
        query = query.where(
            or_(
                effective_activity < cursor_activity,
                and_(
                    effective_activity == cursor_activity,
                    OwnerConversation.id < cursor_id,
                ),
            )
        )
    rows = session.scalars(
        query.order_by(
            func.coalesce(activity, OwnerConversation.created_at).desc(),
            OwnerConversation.id.desc(),
        ).limit(CONVERSATION_PAGE_SIZE + 1)
    ).all()
    page = rows[:CONVERSATION_PAGE_SIZE]
    return ConversationListResponse(
        items=[conversation_response(session, item) for item in page],
        next_cursor=_encode_cursor(page[-1]) if len(rows) > len(page) else None,
    )


def conversation_response(
    session: Session, conversation: OwnerConversation
) -> ConversationResponse:
    latest = session.scalar(
        select(OwnerChatMessage.content)
        .where(OwnerChatMessage.conversation_id == conversation.id)
        .order_by(OwnerChatMessage.sequence_number.desc(), OwnerChatMessage.id.desc())
        .limit(1)
    )
    preview = " ".join(latest.split())[:160] if latest else None
    return ConversationResponse(
        id=conversation.id,
        creator_user_id=conversation.creator_user_id,
        channel="owner_web",
        title=conversation.title,
        next_turn_number=conversation.next_turn_number,
        last_message_at=conversation.last_message_at,
        latest_message_preview=preview,
        archived=conversation.archived,
        archived_at=conversation.archived_at,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )
