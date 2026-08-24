"""Tenant-scoped WhatsApp connection and customer-conversation management."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime

from fastapi import status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.channels.meta import MetaWhatsAppAdapter
from app.channels.profiles import ChannelProfileRegistry, ChannelProfileUnavailable
from app.core.exceptions import ApplicationError
from app.core.security import utc_now
from app.database.models import (
    BusinessStatus,
    CustomerConversation,
    CustomerConversationState,
    CustomerMessage,
    CustomerMessageStatus,
    MessagingChannelConnection,
    MessagingConnectionStatus,
    User,
)
from app.schemas.customer_channels import (
    CustomerConversationListResponse,
    CustomerConversationResponse,
    CustomerMessageListResponse,
    CustomerMessageResponse,
    WhatsAppConnectionCreate,
    WhatsAppConnectionResponse,
)
from app.services.businesses import load_full_access_business

PAGE_SIZE = 30


def _error(code: str, http_status: int = 422) -> ApplicationError:
    messages = {
        "channel_not_found": "WhatsApp connection was not found.",
        "channel_profile_unsupported": "This connection profile is unavailable.",
        "channel_profile_unavailable": (
            "The deployment-managed profile is not configured."
        ),
        "channel_not_validated": "Validate the WhatsApp connection before activation.",
        "channel_not_active": "The WhatsApp connection is not active.",
        "customer_conversation_not_found": "Customer conversation was not found.",
        "business_not_active": "The business must be active.",
        "manual_confirmation_required": "Confirm the external message before sending.",
    }
    return ApplicationError(
        messages.get(code, "The messaging request could not be completed."),
        status_code=http_status,
        error_code=code,
    )


def _connection_response(
    connection: MessagingChannelConnection,
) -> WhatsAppConnectionResponse:
    return WhatsAppConnectionResponse.model_validate(connection, from_attributes=True)


def list_connections(
    session: Session, user: User, business_id: uuid.UUID
) -> list[WhatsAppConnectionResponse]:
    load_full_access_business(session, user, business_id)
    rows = session.scalars(
        select(MessagingChannelConnection)
        .where(MessagingChannelConnection.business_id == business_id)
        .order_by(MessagingChannelConnection.created_at, MessagingChannelConnection.id)
    ).all()
    return [_connection_response(row) for row in rows]


def configure_connection(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    body: WhatsAppConnectionCreate,
    registry: ChannelProfileRegistry,
) -> WhatsAppConnectionResponse:
    load_full_access_business(session, user, business_id)
    if body.connection_profile_key not in registry.keys:
        raise _error("channel_profile_unsupported")
    existing = session.scalar(
        select(MessagingChannelConnection).where(
            MessagingChannelConnection.business_id == business_id,
            MessagingChannelConnection.provider_type == "meta_whatsapp",
        )
    )
    if existing is None:
        existing = MessagingChannelConnection(
            id=uuid.uuid4(),
            business_id=business_id,
            provider_type="meta_whatsapp",
            display_name=body.display_name,
            connection_profile_key=body.connection_profile_key,
            status=MessagingConnectionStatus.CONFIGURED,
        )
        session.add(existing)
    else:
        if existing.status == MessagingConnectionStatus.ACTIVE:
            raise _error("channel_active", status.HTTP_409_CONFLICT)
        existing.display_name = body.display_name
        existing.connection_profile_key = body.connection_profile_key
        existing.status = MessagingConnectionStatus.CONFIGURED
        existing.external_phone_number_id = None
        existing.last_validated_at = None
        existing.last_successful_health_check_at = None
        existing.failure_code = None
    session.commit()
    return _connection_response(existing)


def _load_connection(
    session: Session,
    business_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    locked: bool = False,
) -> MessagingChannelConnection:
    query = select(MessagingChannelConnection).where(
        MessagingChannelConnection.id == connection_id,
        MessagingChannelConnection.business_id == business_id,
    )
    if locked:
        query = query.with_for_update()
    connection = session.scalar(query)
    if connection is None:
        raise _error("channel_not_found", status.HTTP_404_NOT_FOUND)
    return connection


def _adapter(
    registry: ChannelProfileRegistry, connection: MessagingChannelConnection
) -> MetaWhatsAppAdapter:
    try:
        profile = registry.resolve(connection.connection_profile_key)
    except ChannelProfileUnavailable as exc:
        code = (
            "channel_profile_unsupported"
            if str(exc) == "unsupported_profile"
            else "channel_profile_unavailable"
        )
        raise _error(code, status.HTTP_503_SERVICE_UNAVAILABLE) from None
    return MetaWhatsAppAdapter(profile)


def validate_connection(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    connection_id: uuid.UUID,
    registry: ChannelProfileRegistry,
) -> WhatsAppConnectionResponse:
    load_full_access_business(session, user, business_id)
    connection = _load_connection(session, business_id, connection_id)
    adapter = _adapter(registry, connection)
    result = adapter.validate_connection()
    now = utc_now()
    locked = _load_connection(session, business_id, connection_id, locked=True)
    locked.last_validated_at = now
    if result.healthy:
        locked.status = MessagingConnectionStatus.VALIDATED
        locked.last_successful_health_check_at = now
        locked.external_phone_number_id = adapter.profile.phone_number_id
        locked.failure_code = None
    else:
        locked.status = MessagingConnectionStatus.UNHEALTHY
        locked.failure_code = result.failure_code or "channel.validation_failed"
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise _error("channel_profile_in_use", status.HTTP_409_CONFLICT) from None
    return _connection_response(locked)


def activate_connection(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    connection_id: uuid.UUID,
) -> WhatsAppConnectionResponse:
    business = load_full_access_business(session, user, business_id, for_update=True)
    if business.status is not BusinessStatus.ACTIVE:
        raise _error("business_not_active", status.HTTP_403_FORBIDDEN)
    connection = _load_connection(session, business_id, connection_id, locked=True)
    if connection.status == MessagingConnectionStatus.ACTIVE:
        session.commit()
        return _connection_response(connection)
    if connection.status != MessagingConnectionStatus.VALIDATED:
        session.rollback()
        raise _error("channel_not_validated", status.HTTP_409_CONFLICT)
    connection.status = MessagingConnectionStatus.ACTIVE
    connection.auto_reply_enabled = True
    connection.failure_code = None
    session.commit()
    return _connection_response(connection)


def health_connection(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    connection_id: uuid.UUID,
    registry: ChannelProfileRegistry,
) -> WhatsAppConnectionResponse:
    load_full_access_business(session, user, business_id)
    snapshot = _load_connection(session, business_id, connection_id)
    result = _adapter(registry, snapshot).validate_connection()
    now = utc_now()
    connection = _load_connection(session, business_id, connection_id, locked=True)
    connection.last_validated_at = now
    if result.healthy:
        connection.last_successful_health_check_at = now
        connection.failure_code = None
        if connection.status == MessagingConnectionStatus.UNHEALTHY:
            connection.status = MessagingConnectionStatus.VALIDATED
    else:
        connection.status = MessagingConnectionStatus.UNHEALTHY
        connection.failure_code = result.failure_code or "channel.health_failed"
    session.commit()
    return _connection_response(connection)


def disable_connection(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    connection_id: uuid.UUID,
) -> WhatsAppConnectionResponse:
    load_full_access_business(session, user, business_id)
    connection = _load_connection(session, business_id, connection_id, locked=True)
    connection.status = MessagingConnectionStatus.DISABLED
    connection.auto_reply_enabled = False
    connection.failure_code = None
    session.commit()
    return _connection_response(connection)


def set_auto_reply(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    connection_id: uuid.UUID,
    enabled: bool,
) -> WhatsAppConnectionResponse:
    load_full_access_business(session, user, business_id)
    connection = _load_connection(session, business_id, connection_id, locked=True)
    if connection.status != MessagingConnectionStatus.ACTIVE:
        session.rollback()
        raise _error("channel_not_active", status.HTTP_409_CONFLICT)
    connection.auto_reply_enabled = enabled
    session.commit()
    return _connection_response(connection)


def _encode_cursor(created_at: datetime, identifier: uuid.UUID) -> str:
    raw = json.dumps([created_at.astimezone(UTC).isoformat(), str(identifier)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[datetime, uuid.UUID] | None:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        stamp, identifier = json.loads(raw)
        parsed = datetime.fromisoformat(stamp)
        if parsed.utcoffset() is None:
            raise ValueError
        return parsed, uuid.UUID(identifier)
    except ValueError, TypeError, json.JSONDecodeError:
        raise _error("invalid_cursor") from None


def _load_customer_conversation(
    session: Session,
    business_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    locked: bool = False,
) -> CustomerConversation:
    query = select(CustomerConversation).where(
        CustomerConversation.id == conversation_id,
        CustomerConversation.business_id == business_id,
    )
    if locked:
        query = query.with_for_update()
    conversation = session.scalar(query)
    if conversation is None:
        raise _error("customer_conversation_not_found", status.HTTP_404_NOT_FOUND)
    return conversation


def list_customer_conversations(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    cursor: str | None,
) -> CustomerConversationListResponse:
    load_full_access_business(session, user, business_id)
    query = select(CustomerConversation).where(
        CustomerConversation.business_id == business_id
    )
    decoded = _decode_cursor(cursor)
    activity = func.coalesce(
        CustomerConversation.last_message_at, CustomerConversation.created_at
    )
    if decoded:
        stamp, identifier = decoded
        query = query.where(
            or_(
                activity < stamp,
                (activity == stamp) & (CustomerConversation.id < identifier),
            )
        )
    rows = session.scalars(
        query.order_by(activity.desc(), CustomerConversation.id.desc()).limit(
            PAGE_SIZE + 1
        )
    ).all()
    visible = rows[:PAGE_SIZE]
    items: list[CustomerConversationResponse] = []
    for row in visible:
        latest = session.scalar(
            select(CustomerMessage.content)
            .where(CustomerMessage.conversation_id == row.id)
            .order_by(CustomerMessage.created_at.desc(), CustomerMessage.id.desc())
            .limit(1)
        )
        items.append(
            CustomerConversationResponse(
                id=row.id,
                masked_customer_label=row.masked_customer_label,
                state=row.state,
                last_message_at=row.last_message_at,
                latest_message_preview=(latest[:160] if latest else None),
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
        )
    next_cursor = None
    if len(rows) > PAGE_SIZE and visible:
        last = visible[-1]
        next_cursor = _encode_cursor(last.last_message_at or last.created_at, last.id)
    return CustomerConversationListResponse(items=items, next_cursor=next_cursor)


def list_customer_messages(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    conversation_id: uuid.UUID,
    cursor: str | None,
) -> CustomerMessageListResponse:
    load_full_access_business(session, user, business_id)
    _load_customer_conversation(session, business_id, conversation_id)
    query = select(CustomerMessage).where(
        CustomerMessage.conversation_id == conversation_id,
        CustomerMessage.business_id == business_id,
    )
    decoded = _decode_cursor(cursor)
    if decoded:
        stamp, identifier = decoded
        query = query.where(
            or_(
                CustomerMessage.created_at < stamp,
                (CustomerMessage.created_at == stamp)
                & (CustomerMessage.id < identifier),
            )
        )
    rows = session.scalars(
        query.order_by(
            CustomerMessage.created_at.desc(), CustomerMessage.id.desc()
        ).limit(PAGE_SIZE + 1)
    ).all()
    visible = rows[:PAGE_SIZE]
    next_cursor = (
        _encode_cursor(visible[-1].created_at, visible[-1].id)
        if len(rows) > PAGE_SIZE and visible
        else None
    )
    return CustomerMessageListResponse(
        items=[
            CustomerMessageResponse.model_validate(row, from_attributes=True)
            for row in reversed(visible)
        ],
        next_cursor=next_cursor,
    )


def set_handoff(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    handoff: bool,
) -> CustomerConversationResponse:
    load_full_access_business(session, user, business_id)
    conversation = _load_customer_conversation(
        session, business_id, conversation_id, locked=True
    )
    conversation.state = (
        CustomerConversationState.HUMAN_HANDOFF
        if handoff
        else CustomerConversationState.AI_ACTIVE
    )
    session.commit()
    return CustomerConversationResponse.model_validate(
        conversation, from_attributes=True
    )


def create_manual_reply(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    conversation_id: uuid.UUID,
    content: str,
) -> CustomerMessageResponse:
    business = load_full_access_business(session, user, business_id)
    if business.status is not BusinessStatus.ACTIVE:
        raise _error("business_not_active", status.HTTP_403_FORBIDDEN)
    conversation = _load_customer_conversation(
        session, business_id, conversation_id, locked=True
    )
    connection = _load_connection(session, business_id, conversation.connection_id)
    if connection.status != MessagingConnectionStatus.ACTIVE:
        raise _error("channel_not_active", status.HTTP_409_CONFLICT)
    message = CustomerMessage(
        id=uuid.uuid4(),
        business_id=business_id,
        conversation_id=conversation.id,
        direction="outbound",
        sender="owner",
        content=content,
        status=CustomerMessageStatus.PENDING_SEND,
    )
    session.add(message)
    conversation.last_message_at = utc_now()
    session.commit()
    from app.worker.customer_messages import enqueue_outbound_message

    try:
        enqueue_outbound_message(message.id)
    except Exception:
        raise _error("queue_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE) from None
    return CustomerMessageResponse.model_validate(message, from_attributes=True)
