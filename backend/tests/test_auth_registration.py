"""Registration, email verification, and resend behavior."""

from datetime import timedelta

import pytest
from app.core.security import token_digest, utc_now, verify_password
from app.database.models import (
    AuthenticationEvent,
    Business,
    BusinessMembership,
    EmailVerificationToken,
    User,
)
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from tests.conftest import MockEmailService

VALID_REGISTRATION = {
    "first_name": "Maya",
    "last_name": "Haddad",
    "email": "owner@example.com",
    "password": "Strong1!Pass",
    "password_confirmation": "Strong1!Pass",
}


def register(client: TestClient, **changes: str) -> object:
    body = VALID_REGISTRATION | changes
    return client.post("/api/v1/auth/register", json=body)


def test_successful_registration_normalizes_and_hashes_credentials(
    api_client: TestClient,
    db_session: Session,
    email_service: MockEmailService,
) -> None:
    response = register(
        api_client,
        first_name="  Maya  ",
        last_name="  Haddad ",
        email="  OWNER@Example.COM ",
    )

    assert response.status_code == 201
    assert "email" in response.json()["message"].lower()
    user = db_session.scalar(select(User))
    assert user is not None
    assert (user.first_name, user.last_name, user.email) == (
        "Maya",
        "Haddad",
        "owner@example.com",
    )
    assert user.password_hash != VALID_REGISTRATION["password"]
    assert verify_password(VALID_REGISTRATION["password"], user.password_hash)
    assert user.email_verified_at is None
    assert db_session.scalar(select(func.count()).select_from(Business)) == 0
    assert db_session.scalar(select(func.count()).select_from(BusinessMembership)) == 0
    assert email_service.verification_messages[0][0] == "owner@example.com"
    raw_token = email_service.verification_messages[0][1]
    stored_token = db_session.scalar(select(EmailVerificationToken))
    assert stored_token is not None
    assert stored_token.token_hash == token_digest(raw_token)
    assert raw_token not in stored_token.token_hash


@pytest.mark.parametrize("verified", [False, True])
def test_duplicate_email_is_a_safe_conflict(
    api_client: TestClient, db_session: Session, verified: bool
) -> None:
    assert register(api_client).status_code == 201
    if verified:
        user = db_session.scalar(select(User))
        user.email_verified_at = utc_now()
        db_session.commit()

    response = register(api_client, email=" OWNER@example.com ")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_already_registered"
    assert db_session.scalar(select(func.count()).select_from(User)) == 1


def test_password_confirmation_error_does_not_echo_password(
    api_client: TestClient,
) -> None:
    response = register(api_client, password_confirmation="Different2@Secret")
    assert response.status_code == 422
    assert "Strong1!Pass" not in response.text
    assert "Different2@Secret" not in response.text


@pytest.mark.parametrize(
    ("password", "policy_code"),
    [
        ("Aa1!", "minimum_length"),
        ("Aa1!" + "x" * 125, "maximum_length"),
        ("lowercase1!", "uppercase_required"),
        ("UPPERCASE1!", "lowercase_required"),
        ("NoNumbers!", "number_required"),
        ("NoSpecial1", "special_character_required"),
        ("MayaStrong1!", "contains_first_name"),
        ("HaddadStrong1!", "contains_last_name"),
        ("OwnerStrong1!", "contains_email_local_part"),
    ],
)
def test_each_password_policy_rule_is_enforced(
    api_client: TestClient, password: str, policy_code: str
) -> None:
    response = register(api_client, password=password, password_confirmation=password)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "password_policy_violation"
    assert policy_code in response.json()["error"]["message"]
    assert password not in response.text


def test_valid_verification_is_single_use_and_enables_login(
    api_client: TestClient,
    db_session: Session,
    email_service: MockEmailService,
) -> None:
    register(api_client)
    assert (
        api_client.post(
            "/api/v1/auth/login",
            json={
                "email": "owner@example.com",
                "password": "Strong1!Pass",
                "keep_me_signed_in": False,
            },
        ).status_code
        == 403
    )
    token = email_service.verification_messages[-1][1]

    verified = api_client.post("/api/v1/auth/verify-email", json={"token": token})
    assert verified.status_code == 200
    assert verified.json()["message"] == "Email verified successfully."
    user = db_session.scalar(select(User))
    assert user.email_verified_at is not None
    assert (
        api_client.post("/api/v1/auth/verify-email", json={"token": token}).json()[
            "error"
        ]["code"]
        == "verification_token_used"
    )
    login_response = api_client.post(
        "/api/v1/auth/login",
        json={"email": "OWNER@example.com", "password": "Strong1!Pass"},
    )
    assert login_response.status_code == 200


def test_invalid_and_expired_verification_tokens_are_rejected(
    api_client: TestClient,
    db_session: Session,
    email_service: MockEmailService,
) -> None:
    invalid = api_client.post(
        "/api/v1/auth/verify-email", json={"token": "not-a-token"}
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "verification_token_invalid"

    register(api_client)
    token = email_service.verification_messages[-1][1]
    stored = db_session.scalar(select(EmailVerificationToken))
    stored.created_at = utc_now() - timedelta(hours=25)
    stored.expires_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    expired = api_client.post("/api/v1/auth/verify-email", json={"token": token})
    assert expired.status_code == 400
    assert expired.json()["error"]["code"] == "verification_token_expired"


def test_resend_invalidates_previous_token_and_enforces_cooldown(
    api_client: TestClient,
    db_session: Session,
    email_service: MockEmailService,
) -> None:
    register(api_client)
    old_token = email_service.verification_messages[-1][1]
    response = api_client.post(
        "/api/v1/auth/resend-verification", json={"email": "OWNER@example.com"}
    )
    assert response.status_code == 200
    new_token = email_service.verification_messages[-1][1]
    assert new_token != old_token
    old_row = db_session.scalar(
        select(EmailVerificationToken).where(
            EmailVerificationToken.token_hash == token_digest(old_token)
        )
    )
    assert old_row.invalidated_at is not None
    assert (
        api_client.post("/api/v1/auth/verify-email", json={"token": old_token}).json()[
            "error"
        ]["code"]
        == "verification_token_used"
    )
    cooldown = api_client.post(
        "/api/v1/auth/resend-verification", json={"email": "owner@example.com"}
    )
    assert cooldown.status_code == 429
    assert cooldown.json()["error"]["code"] == "resend_cooldown"


def test_resend_hourly_limit_and_already_verified_response(
    api_client: TestClient,
    db_session: Session,
) -> None:
    register(api_client)
    for _ in range(5):
        response = api_client.post(
            "/api/v1/auth/resend-verification", json={"email": "owner@example.com"}
        )
        assert response.status_code == 200
        db_session.execute(
            update(AuthenticationEvent)
            .where(AuthenticationEvent.event_type == "verification_resend")
            .values(created_at=utc_now() - timedelta(minutes=2))
        )
        db_session.commit()
    limited = api_client.post(
        "/api/v1/auth/resend-verification", json={"email": "owner@example.com"}
    )
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"

    db_session.execute(
        update(AuthenticationEvent).values(created_at=utc_now() - timedelta(hours=2))
    )
    user = db_session.scalar(select(User))
    user.email_verified_at = utc_now()
    db_session.commit()
    verified = api_client.post(
        "/api/v1/auth/resend-verification", json={"email": "owner@example.com"}
    )
    assert verified.status_code == 400
    assert verified.json()["error"]["code"] == "email_already_verified"
