"""Query-index and retention tests for authentication abuse events."""

from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from app.api import dependencies
from app.core.config import AUTH_EVENT_MINIMUM_RETENTION_HOURS, Settings
from app.database.models import (
    AuthenticationEvent,
    EmailVerificationToken,
    PasswordResetToken,
    RefreshSession,
)
from app.services import auth_event_retention
from app.services.auth_event_retention import (
    AUTH_EVENT_CLEANUP_LOCK_NAME,
    delete_expired_authentication_events,
)
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine, event, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

NEW_INDEXES = {
    "ix_auth_events_type_email_created",
    "ix_auth_events_type_ip_created",
    "ix_auth_events_created",
}


def authentication_event(
    *,
    created_at: datetime,
    event_type: str = "password_reset_request",
    email: str = "retention@example.test",
    client_ip: str = "192.0.2.10",
) -> AuthenticationEvent:
    return AuthenticationEvent(
        event_type=event_type,
        normalized_email=email,
        client_ip=client_ip,
        created_at=created_at,
    )


def test_authentication_event_model_defines_query_specific_indexes() -> None:
    indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in AuthenticationEvent.__table__.indexes
    }
    assert indexes == {
        "ix_auth_events_scope_created": (
            "event_type",
            "normalized_email",
            "client_ip",
            "created_at",
        ),
        "ix_auth_events_type_email_created": (
            "event_type",
            "normalized_email",
            "created_at",
        ),
        "ix_auth_events_type_ip_created": (
            "event_type",
            "client_ip",
            "created_at",
        ),
        "ix_auth_events_created": ("created_at",),
    }


def test_authentication_event_indexes_exist_in_database(
    database_engine: Engine,
) -> None:
    indexes = {
        index["name"]: tuple(index["column_names"])
        for index in inspect(database_engine).get_indexes("authentication_events")
    }
    assert indexes["ix_auth_events_scope_created"] == (
        "event_type",
        "normalized_email",
        "client_ip",
        "created_at",
    )
    assert indexes["ix_auth_events_type_email_created"] == (
        "event_type",
        "normalized_email",
        "created_at",
    )
    assert indexes["ix_auth_events_type_ip_created"] == (
        "event_type",
        "client_ip",
        "created_at",
    )
    assert indexes["ix_auth_events_created"] == ("created_at",)


def test_token_hashes_have_no_redundant_explicit_indexes() -> None:
    for model in (RefreshSession, EmailVerificationToken, PasswordResetToken):
        assert model.__table__.c.token_hash.unique is True
        assert all(
            tuple(column.name for column in index.columns) != ("token_hash",)
            for index in model.__table__.indexes
        )


def test_authentication_event_index_migration_downgrades_only_new_indexes(
    database_engine: Engine,
    alembic_config: Config,
) -> None:
    database_engine.dispose()
    before = {
        index["name"]
        for index in inspect(database_engine).get_indexes("authentication_events")
    }
    try:
        command.downgrade(alembic_config, "20260811_01")
        after_downgrade = {
            index["name"]
            for index in inspect(database_engine).get_indexes("authentication_events")
        }
        assert before - after_downgrade == NEW_INDEXES
        assert after_downgrade == before - NEW_INDEXES
    finally:
        command.upgrade(alembic_config, "head")

    after_upgrade = {
        index["name"]
        for index in inspect(database_engine).get_indexes("authentication_events")
    }
    assert after_upgrade == before


def test_retention_deletes_only_rows_strictly_older_than_cutoff(
    db_session: Session,
) -> None:
    current_time = datetime(2026, 8, 11, 12, tzinfo=UTC)
    cutoff = current_time - timedelta(hours=24)
    db_session.add_all(
        [
            authentication_event(
                created_at=cutoff - timedelta(microseconds=1),
                email="old@example.test",
            ),
            authentication_event(created_at=cutoff, email="boundary@example.test"),
            authentication_event(
                created_at=cutoff + timedelta(microseconds=1),
                email="recent@example.test",
            ),
        ]
    )
    db_session.commit()

    deleted = delete_expired_authentication_events(
        db_session,
        current_time=current_time,
        retention_hours=24,
    )

    assert deleted == 1
    assert set(db_session.scalars(select(AuthenticationEvent.normalized_email))) == {
        "boundary@example.test",
        "recent@example.test",
    }


def test_retention_preserves_every_active_rate_limit_window(
    db_session: Session,
) -> None:
    current_time = datetime(2026, 8, 11, 12, tzinfo=UTC)
    db_session.add_all(
        [
            authentication_event(
                created_at=current_time - timedelta(hours=1),
                event_type="verification_resend",
                email="hour@example.test",
            ),
            authentication_event(
                created_at=current_time - timedelta(minutes=15),
                event_type="login_failure",
                email="block@example.test",
            ),
            authentication_event(
                created_at=current_time - timedelta(hours=3),
                email="expired@example.test",
            ),
        ]
    )
    db_session.commit()

    assert (
        delete_expired_authentication_events(
            db_session,
            current_time=current_time,
            retention_hours=AUTH_EVENT_MINIMUM_RETENTION_HOURS,
        )
        == 1
    )
    assert set(db_session.scalars(select(AuthenticationEvent.normalized_email))) == {
        "hour@example.test",
        "block@example.test",
    }


def test_retention_requires_timezone_aware_utc_input(db_session: Session) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        delete_expired_authentication_events(
            db_session,
            current_time=datetime(2026, 8, 11, 12),
            retention_hours=24,
        )


def test_retention_configuration_default_and_minimum() -> None:
    assert Settings.model_fields["auth_event_retention_hours"].default == 24
    assert (
        Settings(
            auth_event_retention_hours=AUTH_EVENT_MINIMUM_RETENTION_HOURS,
            _env_file=None,
        ).auth_event_retention_hours
        == AUTH_EVENT_MINIMUM_RETENTION_HOURS
    )
    with pytest.raises(ValidationError):
        Settings(auth_event_retention_hours=1, _env_file=None)


def test_cleanup_deletes_at_most_one_bounded_batch(db_session: Session) -> None:
    current_time = datetime(2026, 8, 11, 12, tzinfo=UTC)
    for offset in range(3):
        db_session.add(
            authentication_event(
                created_at=current_time - timedelta(hours=25, minutes=offset),
                email=f"batch-{offset}@example.test",
            )
        )
    db_session.commit()

    assert (
        delete_expired_authentication_events(
            db_session,
            current_time=current_time,
            retention_hours=24,
            batch_size=2,
        )
        == 2
    )
    assert db_session.scalar(select(AuthenticationEvent.id)) is not None


def test_concurrent_cleanup_skips_when_database_lock_is_held(
    database_engine: Engine,
    db_session: Session,
) -> None:
    current_time = datetime(2026, 8, 11, 12, tzinfo=UTC)
    db_session.add(authentication_event(created_at=current_time - timedelta(hours=25)))
    db_session.commit()
    factory = sessionmaker(bind=database_engine, expire_on_commit=False)

    with factory() as lock_session, factory() as cleanup_session:
        lock_session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
            {"lock_name": AUTH_EVENT_CLEANUP_LOCK_NAME},
        )
        assert (
            delete_expired_authentication_events(
                cleanup_session,
                current_time=current_time,
                retention_hours=24,
            )
            == 0
        )
        cleanup_session.rollback()
        assert cleanup_session.scalar(select(AuthenticationEvent.id)) is not None

    assert (
        delete_expired_authentication_events(
            db_session,
            current_time=current_time,
            retention_hours=24,
        )
        == 1
    )


def test_cleanup_does_not_issue_delete_without_expired_rows(
    database_engine: Engine,
    db_session: Session,
) -> None:
    current_time = datetime(2026, 8, 11, 12, tzinfo=UTC)
    db_session.add(authentication_event(created_at=current_time - timedelta(hours=25)))
    db_session.commit()
    delete_statements: list[str] = []

    def capture_delete(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if statement.startswith("DELETE FROM authentication_events"):
            delete_statements.append(statement)

    event.listen(database_engine, "before_cursor_execute", capture_delete)
    try:
        assert (
            delete_expired_authentication_events(
                db_session,
                current_time=current_time,
                retention_hours=24,
            )
            == 1
        )
        db_session.commit()
        assert (
            delete_expired_authentication_events(
                db_session,
                current_time=current_time,
                retention_hours=24,
            )
            == 0
        )
    finally:
        event.remove(database_engine, "before_cursor_execute", capture_delete)

    assert len(delete_statements) == 1


def test_cleanup_failure_is_private_and_does_not_bypass_rate_limit(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_attempts = 0
    warnings: list[tuple[str, tuple[object, ...]]] = []

    def fail_cleanup(*args: object, **kwargs: object) -> int:
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        raise SQLAlchemyError("synthetic cleanup failure")

    monkeypatch.setattr(
        auth_event_retention, "delete_expired_authentication_events", fail_cleanup
    )
    monkeypatch.setattr(
        auth_event_retention.logger,
        "warning",
        lambda message, *args: warnings.append((message, args)),
    )
    assert (
        dependencies.cleanup_authentication_events_best_effort
        is auth_event_retention.cleanup_authentication_events_best_effort
    )
    payload = {"email": "private-retention@example.test"}

    for _ in range(5):
        assert (
            api_client.post("/api/v1/auth/forgot-password", json=payload).status_code
            == 200
        )
    limited = api_client.post("/api/v1/auth/forgot-password", json=payload)

    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert cleanup_attempts == 6
    assert warnings == [("Authentication-event cleanup failed.", ())] * 6
    logged_text = repr(warnings)
    assert payload["email"] not in logged_text
    assert "127.0.0.1" not in logged_text
    assert "synthetic cleanup failure" not in logged_text
