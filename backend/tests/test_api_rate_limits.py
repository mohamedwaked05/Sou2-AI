"""Persistent registration and owner-chat request-limit tests."""

import uuid
from datetime import timedelta

from app.agent.owner_chat_provider import get_owner_chat_provider
from app.core.security import utc_now
from app.database.models import (
    ChatGenerationState,
    ChatMessageRole,
    OwnerChatMessage,
    OwnerChatRateLimitEvent,
    OwnerConversation,
    RegistrationRateLimitEvent,
)
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from tests.test_auth_registration import register
from tests.test_owner_chat import CapturingProvider, active_business, submit


def test_registration_email_limit_counts_successes_and_failures(
    api_client: TestClient, db_session: Session
) -> None:
    assert register(api_client).status_code == 201
    for _ in range(4):
        assert register(api_client).status_code == 409

    blocked = register(api_client)

    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "registration_email_rate_limited"
    assert int(blocked.headers["retry-after"]) > 0
    assert blocked.json()["error"]["request_id"] == blocked.headers["x-request-id"]
    assert (
        db_session.scalar(select(func.count()).select_from(RegistrationRateLimitEvent))
        == 5
    )


def test_registration_ip_windows_and_shared_ip_below_ceiling(
    api_client: TestClient, db_session: Session, migration_engine: Engine
) -> None:
    now = utc_now()
    db_session.add_all(
        [
            RegistrationRateLimitEvent(
                normalized_email=f"prior-{index}@example.com",
                client_ip="127.0.0.1",
                created_at=now - timedelta(minutes=1),
            )
            for index in range(29)
        ]
    )
    db_session.commit()
    allowed = register(api_client, email="shared-ip@example.com")
    assert allowed.status_code == 201

    blocked = register(api_client, email="next-shared-ip@example.com")
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "registration_ip_rate_limited"

    db_session.rollback()
    with migration_engine.begin() as connection:
        connection.execute(
            text("UPDATE registration_rate_limit_events SET created_at=:created_at"),
            {"created_at": now - timedelta(hours=1)},
        )
    existing = db_session.scalars(select(RegistrationRateLimitEvent)).all()
    db_session.add_all(
        [
            RegistrationRateLimitEvent(
                normalized_email=f"daily-{index}@example.com",
                client_ip="127.0.0.1",
                created_at=now - timedelta(hours=1),
            )
            for index in range(100 - len(existing))
        ]
    )
    db_session.commit()
    daily = register(api_client, email="daily-blocked@example.com")
    assert daily.status_code == 429
    assert daily.json()["error"]["code"] == "registration_ip_daily_rate_limited"


def test_registration_admission_precedes_password_hashing_and_delivery(
    api_client: TestClient,
    db_session: Session,
    monkeypatch,
) -> None:
    db_session.add_all(
        [
            RegistrationRateLimitEvent(
                normalized_email="blocked@example.com",
                client_ip="127.0.0.1",
            )
            for _ in range(5)
        ]
    )
    db_session.commit()
    monkeypatch.setattr(
        "app.services.auth.hash_password",
        lambda _value: (_ for _ in ()).throw(AssertionError("hash called")),
    )

    blocked = register(api_client, email="blocked@example.com")

    assert blocked.status_code == 429


def test_owner_chat_minute_limit_and_blocked_idempotent_retry(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(api_client, db_session)
    provider = CapturingProvider()
    app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    for index in range(3):
        response = submit(
            api_client,
            user,
            business["id"],
            f"allowed-{index}",
            key=f"rate-{index}",
        )
        assert response.status_code == 200

    blocked = submit(
        api_client,
        user,
        business["id"],
        "blocked generation",
        key="rate-blocked",
    )
    repeated = submit(
        api_client,
        user,
        business["id"],
        "blocked generation",
        key="rate-blocked",
    )

    assert blocked.status_code == repeated.status_code == 429
    assert blocked.json()["error"]["code"] == "owner_chat_rate_limited"
    assert len(provider.requests) == 3
    assert (
        db_session.scalar(select(func.count()).select_from(OwnerChatRateLimitEvent))
        == 3
    )
    blocked_messages = db_session.scalars(
        select(OwnerChatMessage).where(OwnerChatMessage.content == "blocked generation")
    ).all()
    assert len(blocked_messages) == 1
    assert blocked_messages[0].generation_state == ChatGenerationState.PENDING
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(OwnerChatMessage)
            .where(OwnerChatMessage.reply_to_message_id == blocked_messages[0].id)
        )
        == 0
    )


def test_owner_chat_hour_limit_and_business_isolation(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(api_client, db_session, name="Hourly Market")
    other_user, other = active_business(
        api_client,
        db_session,
        email="other-hourly@example.com",
        name="Other Market",
    )
    conversation = db_session.scalar(
        select(OwnerConversation).where(
            OwnerConversation.business_id == uuid.UUID(business["id"])
        )
    )
    marker = OwnerChatMessage(
        conversation_id=conversation.id,
        sequence_number=999,
        role=ChatMessageRole.OWNER,
        content="hourly marker",
        idempotency_key="hourly-marker",
        generation_state=ChatGenerationState.FAILED,
    )
    db_session.add(marker)
    db_session.flush()
    db_session.add_all(
        [
            OwnerChatRateLimitEvent(
                business_id=conversation.business_id,
                owner_message_id=marker.id,
                generation_attempt=index + 1,
                created_at=utc_now() - timedelta(minutes=2),
            )
            for index in range(20)
        ]
    )
    db_session.commit()

    blocked = submit(
        api_client,
        user,
        business["id"],
        "hourly blocked",
        key="hourly-blocked",
    )
    allowed_other = submit(
        api_client,
        other_user,
        other["id"],
        "isolated allowance",
        key="other-allowed",
    )

    assert blocked.status_code == 429
    assert allowed_other.status_code == 200
