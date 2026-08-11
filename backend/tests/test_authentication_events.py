"""Query-index and retention tests for authentication abuse events."""

from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from app.api import dependencies
from app.core.config import AUTH_EVENT_MINIMUM_RETENTION_HOURS, Settings
from app.core.security import utc_now
from app.database.models import (
    AuthenticationEvent,
    AuthenticationMaintenanceTask,
    EmailVerificationToken,
    PasswordResetToken,
    RefreshSession,
)
from app.database.session import get_engine
from app.services import auth_event_retention
from app.services.auth_event_retention import (
    AUTH_EVENT_CLEANUP_BATCH_SIZE,
    AUTH_EVENT_CLEANUP_LOCK_NAME,
    AUTH_EVENT_CLEANUP_TASK_NAME,
    claim_authentication_event_cleanup,
    cleanup_authentication_events_best_effort,
    delete_expired_authentication_events,
)
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine, delete, event, inspect, select, text
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


def test_authentication_maintenance_model_has_one_unique_task_key() -> None:
    table = AuthenticationMaintenanceTask.__table__
    assert tuple(column.name for column in table.primary_key.columns) == ("id",)
    assert table.c.task_name.unique is True
    assert table.c.next_run_at.type.timezone is True
    assert table.indexes == set()


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


def test_cleanup_throttle_migration_round_trip(
    database_engine: Engine,
    alembic_config: Config,
) -> None:
    database_engine.dispose()
    assert (
        "authentication_maintenance_tasks" in inspect(database_engine).get_table_names()
    )
    authentication_indexes = {
        index["name"]
        for index in inspect(database_engine).get_indexes("authentication_events")
    }
    try:
        command.downgrade(alembic_config, "20260811_02")
        assert (
            "authentication_maintenance_tasks"
            not in inspect(database_engine).get_table_names()
        )
        assert {
            index["name"]
            for index in inspect(database_engine).get_indexes("authentication_events")
        } == authentication_indexes
    finally:
        command.upgrade(alembic_config, "head")

    inspector = inspect(database_engine)
    assert "authentication_maintenance_tasks" in inspector.get_table_names()
    unique_constraints = inspector.get_unique_constraints(
        "authentication_maintenance_tasks"
    )
    assert any(
        constraint["column_names"] == ["task_name"] for constraint in unique_constraints
    )


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


def test_cleanup_interval_configuration_default_and_validation() -> None:
    assert Settings.model_fields["auth_event_cleanup_interval_minutes"].default == 60
    assert (
        Settings(
            auth_event_cleanup_interval_minutes=1, _env_file=None
        ).auth_event_cleanup_interval_minutes
        == 1
    )
    with pytest.raises(ValidationError):
        Settings(auth_event_cleanup_interval_minutes=0, _env_file=None)


def test_cleanup_claim_rejects_invalid_time_and_interval(db_session: Session) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        claim_authentication_event_cleanup(
            db_session,
            current_time=datetime(2026, 8, 11, 12),
            interval_minutes=60,
        )
    with pytest.raises(ValueError, match="positive"):
        claim_authentication_event_cleanup(
            db_session,
            current_time=datetime(2026, 8, 11, 12, tzinfo=UTC),
            interval_minutes=0,
        )


def test_cleanup_deletes_at_most_one_bounded_batch(db_session: Session) -> None:
    current_time = datetime(2026, 8, 11, 12, tzinfo=UTC)
    for offset in range(AUTH_EVENT_CLEANUP_BATCH_SIZE + 1):
        db_session.add(
            authentication_event(
                created_at=current_time - timedelta(hours=25, microseconds=offset),
                email=f"batch-{offset}@example.test",
            )
        )
    db_session.commit()

    assert (
        delete_expired_authentication_events(
            db_session,
            current_time=current_time,
            retention_hours=24,
        )
        == AUTH_EVENT_CLEANUP_BATCH_SIZE
    )
    assert len(list(db_session.scalars(select(AuthenticationEvent.id)))) == 1


def test_cleanup_claim_persists_across_sessions_and_expires_on_boundary(
    database_engine: Engine,
) -> None:
    current_time = datetime(2026, 8, 11, 12, tzinfo=UTC)
    factory = sessionmaker(bind=database_engine, expire_on_commit=False)
    with database_engine.begin() as connection:
        connection.execute(delete(AuthenticationMaintenanceTask))

    with factory() as first_process:
        assert claim_authentication_event_cleanup(
            first_process,
            current_time=current_time,
            interval_minutes=60,
        )
        first_process.commit()

    with factory() as restarted_process:
        assert not claim_authentication_event_cleanup(
            restarted_process,
            current_time=current_time + timedelta(minutes=59, seconds=59),
            interval_minutes=60,
        )
        restarted_process.rollback()

    with factory() as eligible_process:
        assert claim_authentication_event_cleanup(
            eligible_process,
            current_time=current_time + timedelta(minutes=60),
            interval_minutes=60,
        )
        task = eligible_process.scalar(
            select(AuthenticationMaintenanceTask).where(
                AuthenticationMaintenanceTask.task_name == AUTH_EVENT_CLEANUP_TASK_NAME
            )
        )
        assert task is not None
        assert task.next_run_at == current_time + timedelta(minutes=120)
        assert task.next_run_at.tzinfo is not None


def test_concurrent_cleanup_claim_returns_without_waiting(
    database_engine: Engine,
) -> None:
    current_time = datetime(2026, 8, 11, 12, tzinfo=UTC)
    factory = sessionmaker(bind=database_engine, expire_on_commit=False)
    with database_engine.begin() as connection:
        connection.execute(delete(AuthenticationMaintenanceTask))

    with factory() as claiming_worker, factory() as competing_worker:
        assert claim_authentication_event_cleanup(
            claiming_worker,
            current_time=current_time,
            interval_minutes=60,
        )
        competing_worker.execute(text("SET LOCAL statement_timeout = '100ms'"))
        assert not claim_authentication_event_cleanup(
            competing_worker,
            current_time=current_time,
            interval_minutes=60,
        )
        competing_worker.rollback()
        claiming_worker.commit()

    with factory() as observer:
        assert observer.scalar(select(AuthenticationMaintenanceTask.task_name)) == (
            AUTH_EVENT_CLEANUP_TASK_NAME
        )


def test_throttle_skips_expired_event_queries_until_interval_is_due(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_time = datetime(2026, 8, 11, 12, tzinfo=UTC)
    db_session.execute(delete(AuthenticationMaintenanceTask))
    db_session.add_all(
        [
            authentication_event(
                created_at=current_time - timedelta(hours=25),
                email="automatic-old@example.test",
            ),
            authentication_event(
                created_at=current_time - timedelta(hours=23),
                email="automatic-recent@example.test",
            ),
        ]
    )
    db_session.commit()
    cleanup_engine = get_engine()
    authentication_event_queries: list[str] = []

    def capture_authentication_event_query(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if "authentication_events" in statement:
            authentication_event_queries.append(statement)

    settings = Settings(auth_event_cleanup_interval_minutes=60, _env_file=None)
    monkeypatch.setattr(auth_event_retention, "utc_now", lambda: current_time)
    event.listen(
        cleanup_engine,
        "before_cursor_execute",
        capture_authentication_event_query,
    )
    try:
        assert cleanup_authentication_events_best_effort(settings) == 1
        first_query_count = len(authentication_event_queries)
        assert first_query_count > 0
        assert set(
            db_session.scalars(select(AuthenticationEvent.normalized_email))
        ) == {"automatic-recent@example.test"}

        assert cleanup_authentication_events_best_effort(settings) == 0
        assert cleanup_authentication_events_best_effort(settings) == 0
        assert len(authentication_event_queries) == first_query_count

        monkeypatch.setattr(
            auth_event_retention,
            "utc_now",
            lambda: current_time + timedelta(minutes=60),
        )
        assert cleanup_authentication_events_best_effort(settings) == 0
        assert len(authentication_event_queries) == first_query_count + 1
    finally:
        event.remove(
            cleanup_engine,
            "before_cursor_execute",
            capture_authentication_event_query,
        )


def test_first_eligible_authentication_request_claims_and_cleans(
    api_client: TestClient,
    db_session: Session,
) -> None:
    old_event = authentication_event(created_at=utc_now() - timedelta(hours=25))
    db_session.add(old_event)
    db_session.commit()
    old_event_id = old_event.id

    response = api_client.post(
        "/api/v1/auth/forgot-password",
        json={"email": "first-cleanup@example.test"},
    )

    assert response.status_code == 200
    db_session.expire_all()
    assert db_session.get(AuthenticationEvent, old_event_id) is None
    task = db_session.scalar(
        select(AuthenticationMaintenanceTask).where(
            AuthenticationMaintenanceTask.task_name == AUTH_EVENT_CLEANUP_TASK_NAME
        )
    )
    assert task is not None
    assert task.next_run_at.tzinfo is not None


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
    current_time = datetime(2026, 8, 11, 12, tzinfo=UTC)

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
    monkeypatch.setattr(auth_event_retention, "utc_now", lambda: current_time)
    assert (
        dependencies.cleanup_authentication_events_best_effort
        is auth_event_retention.cleanup_authentication_events_best_effort
    )
    payload = {"email": "private-retention@example.test"}
    responses = []

    for _ in range(5):
        response = api_client.post("/api/v1/auth/forgot-password", json=payload)
        responses.append(response)
        assert response.status_code == 200
    limited = api_client.post("/api/v1/auth/forgot-password", json=payload)
    responses.append(limited)

    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert cleanup_attempts == 1
    assert warnings == [("Authentication-event cleanup failed.", ())]
    logged_text = repr(warnings)
    assert payload["email"] not in logged_text
    assert "127.0.0.1" not in logged_text
    assert "synthetic cleanup failure" not in logged_text
    response_text = "".join(response.text for response in responses)
    assert payload["email"] not in response_text
    assert "127.0.0.1" not in response_text
    assert "synthetic cleanup failure" not in response_text
