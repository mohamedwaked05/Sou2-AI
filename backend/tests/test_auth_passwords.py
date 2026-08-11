"""Password recovery and authenticated password-change behavior."""

from datetime import timedelta

import pytest
from app.core import security
from app.core.security import (
    hash_password,
    token_digest,
    utc_now,
    verify_password,
)
from app.database.models import (
    AuthenticationEvent,
    PasswordResetToken,
    RefreshSession,
)
from app.services.email import EmailDeliveryError, get_email_service
from argon2 import PasswordHasher as Argon2PasswordHasher
from argon2 import Type
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


def test_verify_password_returns_true_for_correct_password() -> None:
    encoded_hash = hash_password("Correct1!Password")

    assert verify_password("Correct1!Password", encoded_hash) is True


def test_verify_password_returns_false_for_incorrect_password() -> None:
    encoded_hash = hash_password("Correct1!Password")

    assert verify_password("Incorrect2@Password", encoded_hash) is False


@pytest.mark.parametrize(
    "encoded_hash",
    [
        # pwdlib raises routine UnknownHashError when no hasher recognizes the text.
        pytest.param("not-a-supported-password-hash", id="unrecognized"),
        # A bare Argon2 prefix is unidentified and produces routine UnknownHashError.
        pytest.param("$argon2id$", id="missing-parameters"),
        # argon2 raises VerificationError; pwdlib's Argon2 adapter converts it to False.
        pytest.param(
            "$argon2id$v=19$m=65536,t=3,p=4",
            id="missing-salt-and-digest",
        ),
        # Invalid ASCII base64 reaches routine argon2 VerificationError inside pwdlib.
        pytest.param(
            "$argon2id$v=19$m=65536,t=3,p=4$***$***",
            id="invalid-ascii-base64",
        ),
        # Non-ASCII fields would leak UnicodeEncodeError without the ASCII guard.
        pytest.param(
            "$argon2id$v=19$m=65536,t=3,p=4$é$é",
            id="invalid-unicode-encoding",
        ),
        # A truncated digest uses valid characters but yields routine VerificationError.
        pytest.param(
            "$argon2id$v=19$m=65536,t=3,p=4$MDEyMzQ1Njc4OWFiY2RlZg$YWJj",
            id="truncated-digest",
        ),
        # Zero memory cost is rejected with routine argon2 VerificationError.
        pytest.param(
            "$argon2id$v=19$m=0,t=3,p=4$MDEyMzQ1Njc4OWFiY2RlZg$YWJj",
            id="zero-memory-cost",
        ),
        # Zero parallelism is parsed by argon2 and rejected with VerificationError.
        pytest.param(
            "$argon2id$v=19$m=65536,t=3,p=0$MDEyMzQ1Njc4OWFiY2RlZg$YWJj",
            id="zero-parallelism",
        ),
        # Empty hash text is unidentified and produces routine UnknownHashError.
        pytest.param("", id="empty"),
        # Whitespace-only hash text is unidentified and produces UnknownHashError.
        pytest.param("   ", id="whitespace"),
        # Unregistered Bcrypt is rejected with routine UnknownHashError.
        pytest.param(
            "$2b$12$abcdefghijklmnopqrstuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuu",
            id="unregistered-bcrypt",
        ),
    ],
)
def test_verify_password_returns_false_for_invalid_hash(encoded_hash: str) -> None:
    assert verify_password("Candidate1!Password", encoded_hash) is False


def test_verify_password_supports_registered_argon2i_variant() -> None:
    encoded_hash = Argon2PasswordHasher(type=Type.I).hash("Correct1!Password")

    assert verify_password("Correct1!Password", encoded_hash) is True
    assert verify_password("Incorrect2@Password", encoded_hash) is False


def test_verify_password_preserves_none_type_error() -> None:
    with pytest.raises(TypeError, match="hash must be str or bytes"):
        verify_password("Candidate1!Password", None)  # type: ignore[arg-type]


def test_verify_password_does_not_swallow_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_unexpected_error(password: str, encoded_hash: str) -> bool:
        raise RuntimeError("unexpected verification failure")

    monkeypatch.setattr(security.password_hash, "verify", raise_unexpected_error)

    with pytest.raises(RuntimeError, match="unexpected verification failure"):
        verify_password("Candidate1!Password", "stored-password-hash")


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


def assert_change_password_error(
    api_client: TestClient,
    db_session: Session,
    *,
    current: str,
    new: str,
    confirmation: str,
    expected_status: int,
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
    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    assert current not in response.text
    assert new not in response.text
    assert confirmation not in response.text


def test_change_password_incorrect_current_password_returns_400(
    api_client: TestClient, db_session: Session
) -> None:
    assert_change_password_error(
        api_client,
        db_session,
        current="Wrong1!Password",
        new="Completely2@New",
        confirmation="Completely2@New",
        expected_status=400,
        expected_code="current_password_invalid",
    )


def test_change_password_current_password_reuse_returns_400(
    api_client: TestClient, db_session: Session
) -> None:
    assert_change_password_error(
        api_client,
        db_session,
        current=PASSWORD,
        new=PASSWORD,
        confirmation=PASSWORD,
        expected_status=400,
        expected_code="password_reused",
    )


def test_change_password_policy_violation_returns_422(
    api_client: TestClient, db_session: Session
) -> None:
    assert_change_password_error(
        api_client,
        db_session,
        current=PASSWORD,
        new="weak",
        confirmation="weak",
        expected_status=422,
        expected_code="password_policy_violation",
    )


def test_change_password_confirmation_mismatch_returns_422(
    api_client: TestClient, db_session: Session
) -> None:
    assert_change_password_error(
        api_client,
        db_session,
        current=PASSWORD,
        new="Completely2@New",
        confirmation="Different3#Value",
        expected_status=422,
        expected_code="validation_error",
    )
