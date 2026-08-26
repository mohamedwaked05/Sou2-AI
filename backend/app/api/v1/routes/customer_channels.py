"""Meta webhook and authenticated WhatsApp management routes."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.channels.contracts import ChannelError, DeliveryStatusEvent, InboundTextEvent
from app.channels.meta import MetaWhatsAppAdapter
from app.channels.profiles import ChannelProfileRegistry, ChannelProfileUnavailable
from app.core.config import Settings, get_settings
from app.core.exceptions import ApplicationError
from app.database.models import User
from app.database.session import get_db_session
from app.schemas.customer_channels import (
    AutoReplyUpdate,
    CustomerConversationListResponse,
    CustomerConversationResponse,
    CustomerMessageListResponse,
    CustomerMessageResponse,
    ManualReplyRequest,
    WebhookAcceptedResponse,
    WhatsAppConnectionCreate,
    WhatsAppConnectionResponse,
)
from app.services.customer_channels import (
    activate_connection,
    configure_connection,
    create_manual_reply,
    disable_connection,
    health_connection,
    list_connections,
    list_customer_conversations,
    list_customer_messages,
    set_auto_reply,
    set_handoff,
    validate_connection,
)
from app.services.customer_messaging import (
    WebhookTenantUnavailable,
    ingest_delivery_event,
    ingest_inbound_event,
)

router = APIRouter(tags=["whatsapp"])
DatabaseSession = Annotated[Session, Depends(get_db_session)]
AuthenticatedUser = Annotated[User, Depends(get_current_user)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def _registry(settings: Settings) -> ChannelProfileRegistry:
    return ChannelProfileRegistry(settings)


@router.get("/channels/whatsapp/webhook", response_class=Response)
def verify_webhook(
    settings: AppSettings,
    mode: Annotated[str, Query(alias="hub.mode", max_length=30)],
    token: Annotated[str, Query(alias="hub.verify_token", max_length=500)],
    challenge: Annotated[str, Query(alias="hub.challenge", max_length=500)],
) -> Response:
    if mode != "subscribe":
        raise ApplicationError(
            "Webhook verification failed.",
            status_code=403,
            error_code="webhook_verification_failed",
        )
    adapter = None
    configured_adapter = False
    registry = _registry(settings)
    for key in registry.keys:
        try:
            candidate = MetaWhatsAppAdapter(
                registry.resolve(key, require_outbound=False)
            )
        except ChannelProfileUnavailable:
            continue
        configured_adapter = True
        if candidate.verify_challenge_token(token):
            adapter = candidate
            break
    if adapter is None:
        raise ApplicationError(
            "Webhook verification failed."
            if configured_adapter
            else "Webhook verification is unavailable.",
            status_code=403 if configured_adapter else 503,
            error_code="webhook_verification_failed"
            if configured_adapter
            else "webhook_unavailable",
        ) from None
    return Response(challenge, media_type="text/plain")


@router.post("/channels/whatsapp/webhook", response_model=WebhookAcceptedResponse)
async def receive_webhook(
    request: Request,
    session: DatabaseSession,
    settings: AppSettings,
) -> WebhookAcceptedResponse:
    content_type = (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    if content_type != "application/json":
        raise ApplicationError(
            "Webhook content type is unsupported.",
            status_code=415,
            error_code="webhook_content_type",
        )
    declared_size = request.headers.get("content-length")
    if declared_size and (
        not declared_size.isdigit()
        or int(declared_size) > settings.whatsapp_webhook_max_bytes
    ):
        raise ApplicationError(
            "Webhook body is too large.",
            status_code=413,
            error_code="webhook_too_large",
        )
    raw_body = await request.body()
    if len(raw_body) > settings.whatsapp_webhook_max_bytes:
        raise ApplicationError(
            "Webhook body is too large.",
            status_code=413,
            error_code="webhook_too_large",
        )
    adapter = None
    configured_adapter = False
    registry = _registry(settings)
    for key in registry.keys:
        try:
            candidate = MetaWhatsAppAdapter(
                registry.resolve(key, require_outbound=False)
            )
        except ChannelProfileUnavailable:
            continue
        configured_adapter = True
        if candidate.verify_webhook_signature(
            raw_body, request.headers.get("x-hub-signature-256")
        ):
            adapter = candidate
            break
    if adapter is None:
        raise ApplicationError(
            "Webhook signature is invalid."
            if configured_adapter
            else "Webhook is unavailable.",
            status_code=401 if configured_adapter else 503,
            error_code=(
                "webhook_signature_invalid"
                if configured_adapter
                else "webhook_unavailable"
            ),
        ) from None
    try:
        events = adapter.parse_verified_events(raw_body)
    except ChannelError as exc:
        raise ApplicationError(
            "Webhook payload is invalid.", status_code=422, error_code=exc.code
        ) from None
    if len(events) > 100:
        raise ApplicationError(
            "Webhook event count exceeds the limit.",
            status_code=413,
            error_code="webhook_event_limit",
        )
    accepted = 0
    for event in events:
        try:
            if isinstance(event, InboundTextEvent):
                accepted += int(ingest_inbound_event(session, event, settings))
            elif isinstance(event, DeliveryStatusEvent):
                accepted += int(ingest_delivery_event(session, event))
        except WebhookTenantUnavailable:
            session.rollback()
            continue
    return WebhookAcceptedResponse(events=accepted)


management = APIRouter(
    prefix="/businesses/{business_id}/channels/whatsapp", tags=["whatsapp"]
)


@management.get("", response_model=list[WhatsAppConnectionResponse])
def connection_list(
    business_id: uuid.UUID, session: DatabaseSession, user: AuthenticatedUser
) -> list[WhatsAppConnectionResponse]:
    return list_connections(session, user, business_id)


@management.post(
    "", response_model=WhatsAppConnectionResponse, status_code=status.HTTP_201_CREATED
)
def connection_configure(
    business_id: uuid.UUID,
    body: WhatsAppConnectionCreate,
    session: DatabaseSession,
    user: AuthenticatedUser,
    settings: AppSettings,
) -> WhatsAppConnectionResponse:
    return configure_connection(session, user, business_id, body, _registry(settings))


@management.post("/{connection_id}/validate", response_model=WhatsAppConnectionResponse)
def connection_validate(
    business_id: uuid.UUID,
    connection_id: uuid.UUID,
    session: DatabaseSession,
    user: AuthenticatedUser,
    settings: AppSettings,
) -> WhatsAppConnectionResponse:
    return validate_connection(
        session, user, business_id, connection_id, _registry(settings)
    )


@management.post("/{connection_id}/activate", response_model=WhatsAppConnectionResponse)
def connection_activate(
    business_id: uuid.UUID,
    connection_id: uuid.UUID,
    session: DatabaseSession,
    user: AuthenticatedUser,
) -> WhatsAppConnectionResponse:
    return activate_connection(session, user, business_id, connection_id)


@management.post("/{connection_id}/health", response_model=WhatsAppConnectionResponse)
def connection_health(
    business_id: uuid.UUID,
    connection_id: uuid.UUID,
    session: DatabaseSession,
    user: AuthenticatedUser,
    settings: AppSettings,
) -> WhatsAppConnectionResponse:
    return health_connection(
        session, user, business_id, connection_id, _registry(settings)
    )


@management.post("/{connection_id}/disable", response_model=WhatsAppConnectionResponse)
def connection_disable(
    business_id: uuid.UUID,
    connection_id: uuid.UUID,
    session: DatabaseSession,
    user: AuthenticatedUser,
) -> WhatsAppConnectionResponse:
    return disable_connection(session, user, business_id, connection_id)


@management.patch(
    "/{connection_id}/auto-reply", response_model=WhatsAppConnectionResponse
)
def connection_auto_reply(
    business_id: uuid.UUID,
    connection_id: uuid.UUID,
    body: AutoReplyUpdate,
    session: DatabaseSession,
    user: AuthenticatedUser,
) -> WhatsAppConnectionResponse:
    return set_auto_reply(session, user, business_id, connection_id, body.enabled)


@management.get("/conversations", response_model=CustomerConversationListResponse)
def customer_conversation_list(
    business_id: uuid.UUID,
    session: DatabaseSession,
    user: AuthenticatedUser,
    cursor: Annotated[str | None, Query(max_length=500)] = None,
) -> CustomerConversationListResponse:
    return list_customer_conversations(session, user, business_id, cursor)


@management.get(
    "/conversations/{conversation_id}/messages",
    response_model=CustomerMessageListResponse,
)
def customer_message_list(
    business_id: uuid.UUID,
    conversation_id: uuid.UUID,
    session: DatabaseSession,
    user: AuthenticatedUser,
    cursor: Annotated[str | None, Query(max_length=500)] = None,
) -> CustomerMessageListResponse:
    return list_customer_messages(session, user, business_id, conversation_id, cursor)


@management.post(
    "/conversations/{conversation_id}/handoff",
    response_model=CustomerConversationResponse,
)
def customer_handoff(
    business_id: uuid.UUID,
    conversation_id: uuid.UUID,
    session: DatabaseSession,
    user: AuthenticatedUser,
) -> CustomerConversationResponse:
    return set_handoff(session, user, business_id, conversation_id, handoff=True)


@management.post(
    "/conversations/{conversation_id}/resume",
    response_model=CustomerConversationResponse,
)
def customer_resume(
    business_id: uuid.UUID,
    conversation_id: uuid.UUID,
    session: DatabaseSession,
    user: AuthenticatedUser,
) -> CustomerConversationResponse:
    return set_handoff(session, user, business_id, conversation_id, handoff=False)


@management.post(
    "/conversations/{conversation_id}/messages",
    response_model=CustomerMessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def customer_manual_reply(
    business_id: uuid.UUID,
    conversation_id: uuid.UUID,
    body: ManualReplyRequest,
    session: DatabaseSession,
    user: AuthenticatedUser,
) -> CustomerMessageResponse:
    return create_manual_reply(
        session, user, business_id, conversation_id, body.content
    )


router.include_router(management)
