"""Offline security and lifecycle coverage for WhatsApp customer messaging."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
from app.agent.owner_chat_provider import DeterministicMockOwnerChatProvider
from app.channels.contracts import ChannelError, ChannelProfile, SendTextResult
from app.channels.meta import MetaWhatsAppAdapter
from app.channels.privacy import decrypt_identity, encrypt_identity, identity_hash
from app.core.config import Settings, get_settings
from app.database.models import (
    AIUsageReservation,
    CustomerConversation,
    CustomerConversationState,
    CustomerMessage,
    CustomerMessageStatus,
    InboundWebhookDelivery,
    MessagingChannelConnection,
)
from app.main import app
from app.services import customer_messaging
from app.worker import customer_messages
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from tests.test_business_api import (
    change_business_status,
    headers,
)
from tests.test_business_lifecycle import complete_and_confirm

APP_SECRET = "app-secret-for-offline-signature-tests"
IDENTITY_ENCRYPTION_KEY = "identity-encryption-key-for-tests-1234567890"
IDENTITY_HMAC_KEY = "identity-hmac-key-for-tests-123456789012345"


def channel_settings() -> Settings:
    return Settings(
        _env_file=None,
        whatsapp_access_token="offline-access-token",
        meta_app_secret=APP_SECRET,
        whatsapp_webhook_verify_token="offline-verify-token",
        whatsapp_phone_number_id="15550001111",
        customer_identity_encryption_key=IDENTITY_ENCRYPTION_KEY,
        customer_identity_hmac_key=IDENTITY_HMAC_KEY,
    )


def _profile() -> ChannelProfile:
    return ChannelProfile(
        key="meta_whatsapp_cloud",
        provider_type="meta_whatsapp",
        access_token="offline-token",
        app_secret=APP_SECRET,
        verify_token="offline-verify-token",
        phone_number_id="15550001111",
        graph_api_version="v23.0",
        request_timeout_seconds=5,
    )


def _webhook_body(*, message_id: str = "wamid.inbound-1") -> bytes:
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "payload-business-id-is-ignored",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": "15550001111"},
                                "messages": [
                                    {
                                        "from": "96170123456",
                                        "id": message_id,
                                        "timestamp": "1787652000",
                                        "type": "text",
                                        "text": {
                                            "body": "Hello, what time do you open?"
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        },
        separators=(",", ":"),
    ).encode()


def _signature(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _active_channel(
    client: TestClient, session: Session
) -> tuple[object, dict[str, object]]:
    user, business = complete_and_confirm(
        client,
        session,
        email="whatsapp-owner@example.com",
        name="WhatsApp Market",
    )
    change_business_status(session, business["id"], "ACTIVE")
    path = f"/api/v1/businesses/{business['id']}/channels/whatsapp"
    created = client.post(
        path,
        headers=headers(user),
        json={
            "display_name": "Customer WhatsApp",
            "connection_profile_key": "meta_whatsapp_cloud",
        },
    )
    assert created.status_code == 201, created.text
    connection_id = created.json()["id"]
    validated = client.post(f"{path}/{connection_id}/validate", headers=headers(user))
    assert validated.status_code == 200, validated.text
    activated = client.post(f"{path}/{connection_id}/activate", headers=headers(user))
    assert activated.status_code == 200, activated.text
    return user, business


def test_meta_signature_challenge_parsing_and_safe_send_errors() -> None:
    adapter = MetaWhatsAppAdapter(_profile())
    body = _webhook_body()
    assert adapter.verify_challenge_token("offline-verify-token")
    assert not adapter.verify_challenge_token("wrong")
    assert adapter.verify_webhook_signature(body, _signature(body))
    assert not adapter.verify_webhook_signature(body, "sha256=" + "0" * 64)
    event = adapter.parse_verified_events(body)[0]
    assert event.phone_number_id == "15550001111"
    assert event.provider_message_id == "wamid.inbound-1"

    transient = MetaWhatsAppAdapter(
        _profile(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(429, headers={"Retry-After": "7"})
        ),
    )
    with pytest.raises(ChannelError) as error:
        transient.send_text("96170123456", "Hello")
    assert error.value.code == "channel.transient_failure"
    assert error.value.retryable is True
    assert error.value.retry_after_seconds == 7


def test_customer_identity_is_randomized_authenticated_and_masked() -> None:
    settings = channel_settings()
    first = encrypt_identity("96170123456", settings)
    second = encrypt_identity("96170123456", settings)
    assert first != second
    assert "96170123456" not in first
    assert decrypt_identity(first, settings) == "96170123456"
    assert identity_hash("96170123456", settings) == identity_hash(
        "96170123456", settings
    )
    assert "96170123456" not in identity_hash("96170123456", settings)


def test_safe_configuration_rejects_raw_connection_details(
    api_client: TestClient, db_session: Session
) -> None:
    settings = channel_settings()
    app.dependency_overrides[get_settings] = lambda: settings
    user, business = complete_and_confirm(
        api_client,
        db_session,
        email="safe-channel@example.com",
        name="Safe Channel Market",
    )
    path = f"/api/v1/businesses/{business['id']}/channels/whatsapp"
    rejected = api_client.post(
        path,
        headers=headers(user),
        json={
            "display_name": "Unsafe",
            "connection_profile_key": "meta_whatsapp_cloud",
            "access_token": "must-not-be-accepted",
            "url": "https://example.invalid",
        },
    )
    assert rejected.status_code == 422
    created = api_client.post(
        path,
        headers=headers(user),
        json={
            "display_name": "Customer WhatsApp",
            "connection_profile_key": "meta_whatsapp_cloud",
        },
    )
    assert created.status_code == 201
    serialized = created.text.lower()
    for forbidden in (
        "access_token",
        "app_secret",
        "verify_token",
        "password",
        "graph.facebook",
    ):
        assert forbidden not in serialized


def test_verified_webhook_routes_by_phone_and_deduplicates_before_work(
    api_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = channel_settings()
    app.dependency_overrides[get_settings] = lambda: settings
    _active_channel(api_client, db_session)
    queued: list[object] = []
    monkeypatch.setattr(
        customer_messaging,
        "_queue_inbound",
        lambda message_id, _settings: queued.append(message_id),
    )
    body = _webhook_body()
    invalid = api_client.post(
        "/api/v1/channels/whatsapp/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": "bad"},
    )
    assert invalid.status_code == 401
    assert db_session.scalar(select(InboundWebhookDelivery.id)) is None

    valid_headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": _signature(body),
    }
    accepted = api_client.post(
        "/api/v1/channels/whatsapp/webhook", content=body, headers=valid_headers
    )
    duplicate = api_client.post(
        "/api/v1/channels/whatsapp/webhook", content=body, headers=valid_headers
    )
    assert accepted.status_code == 200
    assert accepted.json() == {"status": "accepted", "events": 1}
    assert duplicate.json() == {"status": "accepted", "events": 0}
    assert len(queued) == 2  # retry safely re-enqueues the one durable pending job
    assert len(db_session.scalars(select(InboundWebhookDelivery)).all()) == 1
    assert len(db_session.scalars(select(CustomerMessage)).all()) == 1
    conversation = db_session.scalar(select(CustomerConversation))
    assert conversation is not None
    assert conversation.masked_customer_label == "WhatsApp ••••3456"
    assert "96170123456" not in conversation.encrypted_customer_identity
    assert (
        decrypt_identity(conversation.encrypted_customer_identity, settings)
        == "96170123456"
    )
    connection = db_session.scalar(select(MessagingChannelConnection))
    assert connection is not None
    assert conversation.business_id == connection.business_id


def test_webhook_validates_raw_body_before_parsing(
    api_client: TestClient, db_session: Session
) -> None:
    settings = channel_settings()
    app.dependency_overrides[get_settings] = lambda: settings
    malformed = b"{not-json"
    bad_signature = api_client.post(
        "/api/v1/channels/whatsapp/webhook",
        content=malformed,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": "bad"},
    )
    signed_malformed = api_client.post(
        "/api/v1/channels/whatsapp/webhook",
        content=malformed,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _signature(malformed),
        },
    )
    assert bad_signature.status_code == 401
    assert signed_malformed.status_code == 422


def test_cross_tenant_customer_listing_is_denied(
    api_client: TestClient, db_session: Session
) -> None:
    settings = channel_settings()
    app.dependency_overrides[get_settings] = lambda: settings
    _, business = _active_channel(api_client, db_session)
    foreign_user, _ = complete_and_confirm(
        api_client,
        db_session,
        email="foreign-channel@example.com",
        name="Foreign Channel Market",
    )
    response = api_client.get(
        f"/api/v1/businesses/{business['id']}/channels/whatsapp/conversations",
        headers=headers(foreign_user),
    )
    assert response.status_code == 404


def test_handoff_stops_ai_and_manual_reply_requires_confirmation(
    api_client: TestClient,
    db_session: Session,
    migration_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = channel_settings()
    app.dependency_overrides[get_settings] = lambda: settings
    user, business = _active_channel(api_client, db_session)
    monkeypatch.setattr(customer_messaging, "_queue_inbound", lambda *_args: None)
    outbound: list[object] = []
    monkeypatch.setattr(
        customer_messages,
        "enqueue_outbound_message",
        lambda message_id, *_args, **_kwargs: outbound.append(message_id),
    )
    body = _webhook_body(message_id="wamid.handoff")
    payload = json.loads(body)
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = (
        "بدي احكي مع حدا حقيقي"
    )
    body = json.dumps(payload, separators=(",", ":")).encode()
    response = api_client.post(
        "/api/v1/channels/whatsapp/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _signature(body),
        },
    )
    assert response.status_code == 200
    inbound = db_session.scalar(
        select(CustomerMessage).where(CustomerMessage.direction == "inbound")
    )
    assert inbound is not None
    customer_messages.process_inbound_message(
        str(inbound.id),
        provider=DeterministicMockOwnerChatProvider(),
        settings_override=settings,
    )
    db_session.expire_all()
    conversation = db_session.get(CustomerConversation, inbound.conversation_id)
    assert conversation is not None
    assert conversation.state == CustomerConversationState.HUMAN_HANDOFF
    with Session(migration_engine) as audit_session:
        assert audit_session.scalar(select(AIUsageReservation.id)) is None
    automatic = db_session.scalar(
        select(CustomerMessage).where(CustomerMessage.reply_to_message_id == inbound.id)
    )
    assert automatic is not None
    assert automatic.sender == "ai"
    assert len(outbound) == 1

    path = (
        f"/api/v1/businesses/{business['id']}/channels/whatsapp/"
        f"conversations/{conversation.id}/messages"
    )
    unconfirmed = api_client.post(
        path,
        headers=headers(user),
        json={"content": "A team member will help you shortly.", "confirmed": False},
    )
    confirmed = api_client.post(
        path,
        headers=headers(user),
        json={"content": "A team member will help you shortly.", "confirmed": True},
    )
    assert unconfirmed.status_code == 422
    assert confirmed.status_code == 202
    assert confirmed.json()["sender"] == "owner"
    assert confirmed.json()["status"] == "PENDING_SEND"
    resumed = api_client.post(
        f"/api/v1/businesses/{business['id']}/channels/whatsapp/"
        f"conversations/{conversation.id}/resume",
        headers=headers(user),
    )
    assert resumed.status_code == 200
    assert resumed.json()["state"] == "AI_ACTIVE"


def test_customer_generation_is_accounted_once_and_has_no_owner_context(
    api_client: TestClient,
    db_session: Session,
    migration_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = channel_settings()
    app.dependency_overrides[get_settings] = lambda: settings
    _active_channel(api_client, db_session)
    monkeypatch.setattr(customer_messaging, "_queue_inbound", lambda *_args: None)
    monkeypatch.setattr(
        customer_messages, "enqueue_outbound_message", lambda *_args, **_kwargs: None
    )
    body = _webhook_body(message_id="wamid.customer-ai")
    response = api_client.post(
        "/api/v1/channels/whatsapp/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _signature(body),
        },
    )
    assert response.status_code == 200
    inbound = db_session.scalar(
        select(CustomerMessage).where(
            CustomerMessage.provider_message_id == "wamid.customer-ai"
        )
    )
    assert inbound is not None
    provider = DeterministicMockOwnerChatProvider()
    customer_messages.process_inbound_message(
        str(inbound.id), provider=provider, settings_override=settings
    )
    customer_messages.process_inbound_message(
        str(inbound.id), provider=provider, settings_override=settings
    )
    db_session.expire_all()
    with Session(migration_engine) as audit_session:
        reservations = audit_session.scalars(select(AIUsageReservation)).all()
    replies = db_session.scalars(
        select(CustomerMessage).where(CustomerMessage.reply_to_message_id == inbound.id)
    ).all()
    assert len(reservations) == 1
    assert reservations[0].channel == "whatsapp"
    assert reservations[0].capability == "customer_chat"
    assert reservations[0].customer_message_id == inbound.id
    assert reservations[0].owner_message_id is None
    assert reservations[0].status == "completed"
    assert len(replies) == 1
    assert "owner conversation" not in replies[0].content.lower()


class _TransientAdapter:
    provider_type = "meta_whatsapp"

    def __init__(self) -> None:
        self.calls = 0

    def send_text(self, recipient: str, text: str) -> SendTextResult:
        self.calls += 1
        assert recipient == "96170123456"
        assert text
        raise ChannelError(
            "channel.transient_failure", retryable=True, retry_after_seconds=9
        )


def test_outbox_retries_transient_failure_after_commit(
    api_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = channel_settings()
    app.dependency_overrides[get_settings] = lambda: settings
    user, business = _active_channel(api_client, db_session)
    monkeypatch.setattr(customer_messaging, "_queue_inbound", lambda *_args: None)
    body = _webhook_body(message_id="wamid.manual-outbox")
    assert (
        api_client.post(
            "/api/v1/channels/whatsapp/webhook",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _signature(body),
            },
        ).status_code
        == 200
    )
    conversation = db_session.scalar(select(CustomerConversation))
    assert conversation is not None
    monkeypatch.setattr(
        customer_messages, "enqueue_outbound_message", lambda *_args, **_kwargs: None
    )
    response = api_client.post(
        f"/api/v1/businesses/{business['id']}/channels/whatsapp/"
        f"conversations/{conversation.id}/messages",
        headers=headers(user),
        json={"content": "Confirmed manual response", "confirmed": True},
    )
    assert response.status_code == 202
    outbound_id = response.json()["id"]
    adapter = _TransientAdapter()
    scheduled: list[int] = []
    monkeypatch.setattr(
        customer_messages,
        "enqueue_outbound_message",
        lambda _id, _settings, *, delay_seconds=0: scheduled.append(delay_seconds),
    )
    customer_messages.process_outbound_message(
        outbound_id, adapter=adapter, settings_override=settings
    )
    db_session.expire_all()
    outbound = db_session.get(CustomerMessage, outbound_id)
    assert outbound is not None
    assert adapter.calls == 1
    assert outbound.status == CustomerMessageStatus.PENDING_SEND
    assert outbound.send_attempts == 1
    assert outbound.failure_code == "channel.transient_failure"
    assert scheduled == [9]
