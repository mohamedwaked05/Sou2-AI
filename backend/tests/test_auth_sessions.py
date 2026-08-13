"""Login protection, rotating sessions, logout, and current-user tests."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import jwt
import pytest
from app.core.config import get_settings
from app.core.exceptions import ApplicationError
from app.core.security import hash_password, token_digest, utc_now
from app.database.models import (
    AccountStatus,
    AuthenticationEvent,
    RefreshSession,
    User,
)
from app.services.auth import refresh_access_token
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session, sessionmaker

PASSWORD = "Strong1!Pass"


def create_user(
    session: Session,
    *,
    email: str = "owner@example.com",
    verified: bool = True,
    disabled: bool = False,
) -> User:
    user = User(
        email=email,
        first_name="Maya",
        last_name="Haddad",
        password_hash=hash_password(PASSWORD),
        email_verified_at=utc_now() if verified else None,
        status=AccountStatus.DISABLED if disabled else AccountStatus.ACTIVE,
    )
    session.add(user)
    session.commit()
    return user


def login(
    client: TestClient,
    *,
    email: str = "owner@example.com",
    password: str = PASSWORD,
    remembered: bool = False,
) -> object:
    return client.post(
        "/api/v1/auth/login",
        json={
            "email": email,
            "password": password,
            "keep_me_signed_in": remembered,
        },
    )


def refresh_cookie(response: object) -> str:
    value = response.cookies.get(get_settings().refresh_cookie_name)
    assert value is not None
    return value


def set_refresh_cookie(client: TestClient, value: str) -> None:
    client.cookies.clear()
    client.cookies.set(
        get_settings().refresh_cookie_name,
        value,
        path=get_settings().refresh_cookie_path,
    )


def test_valid_login_has_short_access_token_and_safe_cookie(
    api_client: TestClient, db_session: Session
) -> None:
    create_user(db_session)
    before = utc_now()
    response = login(api_client)

    assert response.status_code == 200
    payload = response.json()
    claims = jwt.decode(payload["access_token"], options={"verify_signature": False})
    assert claims["type"] == "access"
    assert claims["exp"] - claims["iat"] == 15 * 60
    assert "password" not in claims
    assert "token" not in claims
    cookie_header = response.headers["set-cookie"].lower()
    assert "httponly" in cookie_header
    assert "samesite=lax" in cookie_header
    assert "path=/api/v1/auth" in cookie_header
    assert "sou2ai_refresh_token" not in payload
    stored = db_session.scalar(select(RefreshSession))
    raw = refresh_cookie(response)
    assert stored.token_hash == token_digest(raw)
    assert raw not in stored.token_hash
    assert (
        timedelta(hours=23, minutes=59)
        < stored.expires_at - before
        < timedelta(days=1, minutes=1)
    )


def test_remembered_login_and_multiple_device_sessions(
    api_client: TestClient, db_session: Session
) -> None:
    create_user(db_session)
    normal = login(api_client)
    remembered = login(api_client, remembered=True)
    assert normal.status_code == remembered.status_code == 200
    sessions = list(db_session.scalars(select(RefreshSession)))
    assert len(sessions) == 2
    durations = sorted(session.expires_at - session.created_at for session in sessions)
    assert timedelta(hours=23) < durations[0] < timedelta(days=2)
    assert timedelta(days=29) < durations[1] < timedelta(days=31)
    assert sessions[0].session_family_id != sessions[1].session_family_id


@pytest.mark.parametrize(
    ("email", "password"),
    [("missing@example.com", PASSWORD), ("owner@example.com", "Wrong1!Password")],
)
def test_invalid_credentials_use_same_public_error(
    api_client: TestClient, db_session: Session, email: str, password: str
) -> None:
    create_user(db_session)
    response = login(api_client, email=email, password=password)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"
    assert response.json()["error"]["message"] == "Invalid email or password."
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


def test_unverified_and_disabled_accounts_are_blocked(
    api_client: TestClient, db_session: Session
) -> None:
    create_user(db_session, email="unverified@example.com", verified=False)
    create_user(db_session, email="disabled@example.com", disabled=True)
    unverified = login(api_client, email="unverified@example.com")
    disabled = login(api_client, email="disabled@example.com")
    assert unverified.status_code == 403
    assert unverified.json()["error"]["code"] == "email_not_verified"
    assert disabled.status_code == 403
    assert disabled.json()["error"]["code"] == "account_disabled"


def test_disabling_account_blocks_refresh_and_revokes_sessions(
    api_client: TestClient, db_session: Session
) -> None:
    user = create_user(db_session)
    assert login(api_client).status_code == 200
    assert login(api_client).status_code == 200
    user.status = AccountStatus.DISABLED
    db_session.commit()

    response = api_client.post("/api/v1/auth/refresh")

    assert response.status_code == 401
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(RefreshSession)
            .where(RefreshSession.revoked_at.is_(None))
        )
        == 0
    )


def test_login_failures_are_scoped_rate_limited_and_cleared_by_success(
    api_client: TestClient, db_session: Session
) -> None:
    create_user(db_session)
    for _ in range(5):
        assert login(api_client, password="Wrong1!Password").status_code == 401
    blocked = login(api_client)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "login_rate_limited"

    db_session.execute(
        update(AuthenticationEvent).values(created_at=utc_now() - timedelta(minutes=16))
    )
    db_session.commit()
    assert login(api_client).status_code == 200
    assert db_session.scalar(select(func.count()).select_from(AuthenticationEvent)) == 0
    assert (
        login(api_client, email="other@example.com", password="bad").status_code == 401
    )
    assert login(api_client).status_code == 200


def test_refresh_rotates_and_reuse_revokes_the_device_family(
    api_client: TestClient, db_session: Session
) -> None:
    create_user(db_session)
    signed_in = login(api_client)
    old_token = refresh_cookie(signed_in)
    refreshed = api_client.post("/api/v1/auth/refresh")
    assert refreshed.status_code == 200
    replacement = refresh_cookie(refreshed)
    assert replacement != old_token
    old_row = db_session.scalar(
        select(RefreshSession).where(
            RefreshSession.token_hash == token_digest(old_token)
        )
    )
    assert old_row.revoked_at is not None
    assert old_row.replaced_by_id is not None

    set_refresh_cookie(api_client, old_token)
    reused = api_client.post("/api/v1/auth/refresh")
    assert reused.status_code == 401
    assert reused.json()["error"]["code"] == "refresh_token_reused"
    set_refresh_cookie(api_client, replacement)
    assert api_client.post("/api/v1/auth/refresh").status_code == 401


def test_concurrent_refresh_allows_one_rotation_and_revokes_reused_family(
    api_client: TestClient, db_session: Session, database_engine: Engine
) -> None:
    create_user(db_session)
    raw_token = refresh_cookie(login(api_client))
    family_id = db_session.scalar(select(RefreshSession.session_family_id))
    barrier = Barrier(2)
    factory = sessionmaker(bind=database_engine, expire_on_commit=False)

    def attempt_refresh() -> str:
        with factory() as independent_session:
            barrier.wait()
            try:
                refresh_access_token(independent_session, get_settings(), raw_token)
            except ApplicationError as exc:
                return exc.error_code
            return "success"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: attempt_refresh(), range(2)))

    assert sorted(outcomes) == ["refresh_token_reused", "success"]
    db_session.expire_all()
    active_count = db_session.scalar(
        select(func.count())
        .select_from(RefreshSession)
        .where(
            RefreshSession.session_family_id == family_id,
            RefreshSession.revoked_at.is_(None),
        )
    )
    assert active_count == 0


@pytest.mark.parametrize("state", ["expired", "revoked"])
def test_expired_and_revoked_refresh_tokens_are_rejected(
    api_client: TestClient, db_session: Session, state: str
) -> None:
    create_user(db_session)
    login(api_client)
    stored = db_session.scalar(select(RefreshSession))
    if state == "expired":
        stored.created_at = utc_now() - timedelta(days=2)
        stored.expires_at = utc_now() - timedelta(seconds=1)
    else:
        stored.revoked_at = utc_now()
    db_session.commit()
    rejected = api_client.post("/api/v1/auth/refresh")
    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] in {
        "refresh_token_expired",
        "refresh_token_reused",
    }


def test_current_logout_is_idempotent_and_leaves_other_device_active(
    api_client: TestClient, db_session: Session
) -> None:
    create_user(db_session)
    first = refresh_cookie(login(api_client))
    second = refresh_cookie(login(api_client))
    set_refresh_cookie(api_client, first)
    logged_out = api_client.post("/api/v1/auth/logout")
    assert logged_out.status_code == 200
    assert "max-age=0" in logged_out.headers["set-cookie"].lower()
    assert api_client.post("/api/v1/auth/logout").status_code == 200
    set_refresh_cookie(api_client, second)
    assert api_client.post("/api/v1/auth/refresh").status_code == 200


def test_logout_all_revokes_every_session_and_clears_cookie(
    api_client: TestClient, db_session: Session
) -> None:
    create_user(db_session)
    first_login = login(api_client)
    access_token = first_login.json()["access_token"]
    second = refresh_cookie(login(api_client))
    response = api_client.post(
        "/api/v1/auth/logout-all",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 200
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert all(
        session.revoked_at is not None
        for session in db_session.scalars(select(RefreshSession))
    )
    set_refresh_cookie(api_client, second)
    assert api_client.post("/api/v1/auth/refresh").status_code == 401


def test_current_user_accepts_valid_token_and_excludes_sensitive_fields(
    api_client: TestClient, db_session: Session
) -> None:
    user = create_user(db_session)
    token = login(api_client).json()["access_token"]
    response = api_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert response.json()["id"] == str(user.id)
    assert response.json()["email"] == user.email
    assert (
        not {
            "password",
            "password_hash",
            "token_hash",
            "refresh_token",
        }
        & response.json().keys()
    )


@pytest.mark.parametrize(
    "authorization", [None, "Bearer malformed", "Basic credentials"]
)
def test_current_user_rejects_missing_or_invalid_tokens(
    api_client: TestClient, authorization: str | None
) -> None:
    headers = {"Authorization": authorization} if authorization else {}
    response = api_client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 401


def test_current_user_rejects_expired_token_and_disabled_user(
    api_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = create_user(db_session)
    token = login(api_client).json()["access_token"]
    user.status = AccountStatus.DISABLED
    db_session.commit()
    disabled = api_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert disabled.status_code == 403

    user.status = AccountStatus.ACTIVE
    db_session.commit()
    settings = get_settings()
    expired = jwt.encode(
        {
            "sub": str(user.id),
            "type": "access",
            "iat": utc_now() - timedelta(hours=1),
            "exp": utc_now() - timedelta(minutes=30),
            "iss": settings.access_token_issuer,
            "aud": settings.access_token_audience,
        },
        settings.access_token_secret.get_secret_value(),
        algorithm=settings.access_token_algorithm,
    )
    response = api_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {expired}"}
    )
    assert response.status_code == 401
