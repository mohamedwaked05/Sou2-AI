"""Password recovery and authenticated password-change behavior."""

from datetime import timedelta

import pytest
from app.core.security import token_digest, utc_now, verify_password
from app.database.models import (
    AuthenticationEvent,
    PasswordResetToken,
    RefreshSession,
)
from app.services.email import EmailDeliveryError, get_email_service
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.conftest import MockEmailService
from tests.test_auth_sessions import (
    PASSWORD,
    create_user,
    login,
    refresh_cookie,
    set_refresh_cookie,
)


def test_forgot_password_has_identical_public_response_and_mocked_delivery(
    api_client: TestClient,
    db_session: Session,
    email_service: MockEmailService,
) -> None:
    create_user(db_session, verified=False)
    existing = api_client.post(
        "/api/v1/auth/forgot-password", json={"email": " OWNER@example.com "}
    )
    missing = api_client.post(
        "/api/v1/auth/forgot-password", json={"email": "missing@example.com"}
    )
    assert existing.status_code == missing.status_code == 200
    assert existing.json() == missing.json()
    assert existing.json()["message"] == (
        "If an account exists for this email, a password-reset link has been sent."
    )
    assert len(email_service.password_reset_messages) == 1
    recipient, raw_token = email_service.password_reset_messages[0]
    assert recipient == "owner@example.com"
    stored = db_session.scalar(select(PasswordResetToken))
    assert stored.token_hash == token_digest(raw_token)
    assert raw_token not in existing.text


def test_forgot_password_delivery_failure_remains_generic(
    api_client: TestClient, db_session: Session
) -> None:
    create_user(db_session)

    class FailingEmailService:
        def send_verification_email(self, recipient: str, token: str) -> None:
            raise AssertionError

        def send_password_reset_email(self, recipient: str, token: str) -> None:
            raise EmailDeliveryError

    from app.main import app

    app.dependency_overrides[get_email_service] = lambda: FailingEmailService()
    response = api_client.post(
        "/api/v1/auth/forgot-password", json={"email": "owner@example.com"}
    )
    assert response.status_code == 200
    assert "If an account exists" in response.json()["message"]
    assert db_session.scalar(select(PasswordResetToken)) is None


def test_forgot_password_rate_limit_is_email_and_ip_scoped(
    api_client: TestClient, db_session: Session
) -> None:
    for _ in range(5):
        assert (
            api_client.post(
                "/api/v1/auth/forgot-password", json={"email": "missing@example.com"}
            ).status_code
            == 200
        )
    limited = api_client.post(
        "/api/v1/auth/forgot-password", json={"email": "missing@example.com"}
    )
    assert limited.status_code == 429
    assert (
        api_client.post(
            "/api/v1/auth/forgot-password", json={"email": "other@example.com"}
        ).status_code
        == 200
    )
    assert len(list(db_session.scalars(select(AuthenticationEvent)))) == 6


def test_valid_reset_changes_hash_revokes_sessions_and_preserves_verification_state(
    api_client: TestClient,
    db_session: Session,
    email_service: MockEmailService,
) -> None:
    user = create_user(db_session, verified=False)
    user.email_verified_at = utc_now()
    db_session.commit()
    assert login(api_client).status_code == 200
    assert login(api_client).status_code == 200
    user.email_verified_at = None
    db_session.commit()
    api_client.post("/api/v1/auth/forgot-password", json={"email": "owner@example.com"})
    token = email_service.password_reset_messages[-1][1]
    response = api_client.post(
        "/api/v1/auth/reset-password",
        json={
            "token": token,
            "password": "Completely2@New",
            "password_confirmation": "Completely2@New",
        },
    )
    assert response.status_code == 200
    db_session.refresh(user)
    assert verify_password("Completely2@New", user.password_hash)
    assert user.email_verified_at is None
    assert all(
        item.revoked_at is not None
        for item in db_session.scalars(select(RefreshSession))
    )
    assert "access_token" not in response.json()
    assert api_client.cookies.get("sou2ai_refresh_token") is not None


def test_reset_rejects_invalid_expired_and_used_tokens(
    api_client: TestClient,
    db_session: Session,
    email_service: MockEmailService,
) -> None:
    create_user(db_session)
    invalid = api_client.post(
        "/api/v1/auth/reset-password",
        json={
            "token": "invalid",
            "password": "Completely2@New",
            "password_confirmation": "Completely2@New",
        },
    )
    assert invalid.json()["error"]["code"] == "reset_token_invalid"

    api_client.post("/api/v1/auth/forgot-password", json={"email": "owner@example.com"})
    token = email_service.password_reset_messages[-1][1]
    stored = db_session.scalar(select(PasswordResetToken))
    stored.created_at = utc_now() - timedelta(minutes=31)
    stored.expires_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    expired = api_client.post(
        "/api/v1/auth/reset-password",
        json={
            "token": token,
            "password": "Completely2@New",
            "password_confirmation": "Completely2@New",
        },
    )
    assert expired.json()["error"]["code"] == "reset_token_expired"

    stored.expires_at = utc_now() + timedelta(minutes=5)
    db_session.commit()
    assert (
        api_client.post(
            "/api/v1/auth/reset-password",
            json={
                "token": token,
                "password": "Completely2@New",
                "password_confirmation": "Completely2@New",
            },
        ).status_code
        == 200
    )
    used = api_client.post(
        "/api/v1/auth/reset-password",
        json={
            "token": token,
            "password": "Another3#Password",
            "password_confirmation": "Another3#Password",
        },
    )
    assert used.json()["error"]["code"] == "reset_token_used"


@pytest.mark.parametrize(
    "password",
    ["weak", "MayaStrong1!", "OwnerStrong1!", "NoSpecial1"],
)
def test_reset_applies_complete_password_policy(
    api_client: TestClient,
    db_session: Session,
    email_service: MockEmailService,
    password: str,
) -> None:
    create_user(db_session)
    api_client.post("/api/v1/auth/forgot-password", json={"email": "owner@example.com"})
    token = email_service.password_reset_messages[-1][1]
    response = api_client.post(
        "/api/v1/auth/reset-password",
        json={
            "token": token,
            "password": password,
            "password_confirmation": password,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "password_policy_violation"
    assert password not in response.text


def test_change_password_keeps_current_family_and_revokes_other_devices(
    api_client: TestClient, db_session: Session
) -> None:
    user = create_user(db_session)
    first_login = login(api_client)
    first_access = first_login.json()["access_token"]
    first_refresh = refresh_cookie(first_login)
    second_refresh = refresh_cookie(login(api_client))
    set_refresh_cookie(api_client, first_refresh)
    response = api_client.post(
        "/api/v1/auth/change-password",
        headers={"Authorization": f"Bearer {first_access}"},
        json={
            "current_password": PASSWORD,
            "new_password": "Completely2@New",
            "new_password_confirmation": "Completely2@New",
        },
    )
    assert response.status_code == 200
    db_session.refresh(user)
    assert verify_password("Completely2@New", user.password_hash)
    first_session = db_session.scalar(
        select(RefreshSession).where(
            RefreshSession.token_hash == token_digest(first_refresh)
        )
    )
    second_session = db_session.scalar(
        select(RefreshSession).where(
            RefreshSession.token_hash == token_digest(second_refresh)
        )
    )
    assert first_session.revoked_at is None
    assert second_session.revoked_at is not None
    assert login(api_client, password=PASSWORD).status_code == 401
    assert login(api_client, password="Completely2@New").status_code == 200


@pytest.mark.parametrize(
    ("current", "new", "confirmation", "expected_code"),
    [
        (
            "Wrong1!Password",
            "Completely2@New",
            "Completely2@New",
            "current_password_invalid",
        ),
        (PASSWORD, PASSWORD, PASSWORD, "password_reused"),
        (PASSWORD, "weak", "weak", "password_policy_violation"),
        (PASSWORD, "Completely2@New", "Different3#Value", "validation_error"),
    ],
)
def test_change_password_validation_errors(
    api_client: TestClient,
    db_session: Session,
    current: str,
    new: str,
    confirmation: str,
    expected_code: str,
) -> None:
    create_user(db_session)
    signed_in = login(api_client)
    response = api_client.post(
        "/api/v1/auth/change-password",
        headers={"Authorization": f"Bearer {signed_in.json()['access_token']}"},
        json={
            "current_password": current,
            "new_password": new,
            "new_password_confirmation": confirmation,
        },
    )
    assert response.status_code in {400, 422}
    assert response.json()["error"]["code"] == expected_code
    assert current not in response.text
    assert new not in response.text
    assert confirmation not in response.text
