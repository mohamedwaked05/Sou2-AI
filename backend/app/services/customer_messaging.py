"""Verified inbound normalization and persisted customer-message intake."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.channels.contracts import DeliveryStatusEvent, InboundTextEvent
from app.channels.privacy import encrypt_identity, identity_hash, masked_identity
from app.core.config import Settings
from app.core.security import utc_now
from app.database.models import (
    CustomerConversation,
    CustomerConversationState,
    CustomerMessage,
    CustomerMessageStatus,
    InboundWebhookDelivery,
    MessagingChannelConnection,
    MessagingConnectionStatus,
)

_DELIVERY_RANK = {
    CustomerMessageStatus.SENT: 1,
    CustomerMessageStatus.DELIVERED: 2,
    CustomerMessageStatus.READ: 3,
    CustomerMessageStatus.FAILED: 4,
}


class WebhookTenantUnavailable(Exception):
    pass


def _active_connection(
    session: Session, phone_number_id: str
) -> MessagingChannelConnection:
    connection = session.scalar(
        select(MessagingChannelConnection).where(
            MessagingChannelConnection.external_phone_number_id == phone_number_id,
            MessagingChannelConnection.provider_type == "meta_whatsapp",
            MessagingChannelConnection.status == MessagingConnectionStatus.ACTIVE,
        )
    )
    if connection is None:
        raise WebhookTenantUnavailable("channel_not_active")
    return connection


def _queue_inbound(message_id: uuid.UUID, settings: Settings) -> None:
    from app.worker.customer_messages import enqueue_inbound_message

    enqueue_inbound_message(message_id, settings)


def ingest_inbound_event(
    session: Session, event: InboundTextEvent, settings: Settings
) -> bool:
    """Persist one normalized message exactly once before queueing it."""
    connection = _active_connection(session, event.phone_number_id)
    existing = session.scalar(
        select(InboundWebhookDelivery).where(
            InboundWebhookDelivery.provider_event_id == event.provider_event_id
        )
    )
    if existing is not None:
        message_id = existing.customer_message_id
        should_queue = existing.status == "QUEUED" and message_id is not None
        session.commit()
        if should_queue:
            _queue_inbound(message_id, settings)
        return False

    customer_hash = identity_hash(event.customer_identity, settings)
    conversation = session.scalar(
        select(CustomerConversation).where(
            CustomerConversation.connection_id == connection.id,
            CustomerConversation.customer_identity_hash == customer_hash,
        )
    )
    if conversation is None:
        conversation = CustomerConversation(
            id=uuid.uuid4(),
            business_id=connection.business_id,
            connection_id=connection.id,
            customer_identity_hash=customer_hash,
            encrypted_customer_identity=encrypt_identity(
                event.customer_identity, settings
            ),
            masked_customer_label=masked_identity(event.customer_identity),
            state=CustomerConversationState.AI_ACTIVE,
            last_message_at=event.timestamp,
        )
        try:
            with session.begin_nested():
                session.add(conversation)
                session.flush()
        except IntegrityError:
            conversation = session.scalar(
                select(CustomerConversation).where(
                    CustomerConversation.connection_id == connection.id,
                    CustomerConversation.customer_identity_hash == customer_hash,
                )
            )
            if conversation is None:
                raise

    message = CustomerMessage(
        id=uuid.uuid4(),
        business_id=connection.business_id,
        conversation_id=conversation.id,
        direction="inbound",
        sender="customer",
        content=event.text,
        status=CustomerMessageStatus.RECEIVED,
        provider_message_id=event.provider_message_id,
        provider_timestamp=event.timestamp,
    )
    delivery = InboundWebhookDelivery(
        id=uuid.uuid4(),
        business_id=connection.business_id,
        connection_id=connection.id,
        provider_event_id=event.provider_event_id,
        event_kind="message",
        status="QUEUED",
        customer_message_id=message.id,
    )
    try:
        with session.begin_nested():
            session.add_all((message, delivery))
            session.flush()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(
            select(InboundWebhookDelivery).where(
                InboundWebhookDelivery.provider_event_id == event.provider_event_id
            )
        )
        if existing is None:
            raise
        if existing.customer_message_id is not None and existing.status == "QUEUED":
            _queue_inbound(existing.customer_message_id, settings)
        return False
    conversation.last_message_at = max(
        filter(None, (conversation.last_message_at, event.timestamp))
    )
    session.commit()
    _queue_inbound(message.id, settings)
    return True


def ingest_delivery_event(session: Session, event: DeliveryStatusEvent) -> bool:
    connection = _active_connection(session, event.phone_number_id)
    if session.scalar(
        select(InboundWebhookDelivery.id).where(
            InboundWebhookDelivery.provider_event_id == event.provider_event_id
        )
    ):
        session.commit()
        return False
    message = session.scalar(
        select(CustomerMessage)
        .where(
            CustomerMessage.business_id == connection.business_id,
            CustomerMessage.provider_message_id == event.provider_message_id,
            CustomerMessage.direction == "outbound",
        )
        .with_for_update()
    )
    delivery = InboundWebhookDelivery(
        id=uuid.uuid4(),
        business_id=connection.business_id,
        connection_id=connection.id,
        provider_event_id=event.provider_event_id,
        event_kind="status",
        status="PROCESSED" if message else "IGNORED",
        customer_message_id=message.id if message else None,
        processed_at=utc_now(),
    )
    session.add(delivery)
    if message is not None:
        target = CustomerMessageStatus(event.status.upper())
        current_rank = _DELIVERY_RANK.get(message.status, 0)
        if _DELIVERY_RANK[target] >= current_rank:
            message.status = target
            message.failure_code = (
                event.failure_code if target == CustomerMessageStatus.FAILED else None
            )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return False
    return True
