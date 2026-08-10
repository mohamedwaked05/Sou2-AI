"""PostgreSQL tests for the privacy-minimal tool-call audit table."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.database.models import Business, ToolCallLog, ToolCallStatus, User
from app.services.tool_call_audit import delete_expired_tool_call_logs
from sqlalchemy import Engine, delete, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

VALID_DIGEST = "a" * 64


def business() -> Business:
    return Business(name="Audit Scope")


def user() -> User:
    return User(
        email="audit@example.com",
        first_name="Audit",
        last_name="Owner",
        password_hash="hash",
    )


@pytest.mark.parametrize(
    ("audit_status", "error_code"),
    [
        (ToolCallStatus.SUCCESS, None),
        (ToolCallStatus.ERROR, "adapter.timeout"),
        (ToolCallStatus.DENIED, "permission.denied"),
    ],
)
def test_valid_audit_outcomes(
    db_session: Session,
    audit_status: ToolCallStatus,
    error_code: str | None,
) -> None:
    scoped_business = business()
    log = ToolCallLog(
        business=scoped_business,
        tool_name="inventory.lookup",
        args_hash=VALID_DIGEST,
        status=audit_status,
        error_code=error_code,
        latency_ms=15,
    )
    db_session.add(log)
    db_session.commit()
    assert log.id is not None


@pytest.mark.parametrize(
    ("audit_status", "error_code"),
    [
        (ToolCallStatus.SUCCESS, "unexpected.error"),
        (ToolCallStatus.ERROR, None),
        (ToolCallStatus.DENIED, "   "),
    ],
)
def test_invalid_status_error_code_combinations_are_rejected(
    db_session: Session,
    audit_status: ToolCallStatus,
    error_code: str | None,
) -> None:
    db_session.add(
        ToolCallLog(
            business=business(),
            tool_name="catalog.read",
            args_hash=VALID_DIGEST,
            status=audit_status,
            error_code=error_code,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_negative_latency_is_rejected(db_session: Session) -> None:
    db_session.add(
        ToolCallLog(
            business=business(),
            tool_name="catalog.read",
            args_hash=VALID_DIGEST,
            status=ToolCallStatus.SUCCESS,
            latency_ms=-1,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_required_business_scope_is_enforced(db_session: Session) -> None:
    db_session.add(
        ToolCallLog(
            business_id=uuid.uuid4(),
            tool_name="catalog.read",
            args_hash=VALID_DIGEST,
            status=ToolCallStatus.SUCCESS,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_deleting_user_nulls_audit_user_id(db_session: Session) -> None:
    audit_user = user()
    log = ToolCallLog(
        business=business(),
        user=audit_user,
        tool_name="catalog.read",
        args_hash=VALID_DIGEST,
        status=ToolCallStatus.SUCCESS,
    )
    db_session.add(log)
    db_session.commit()
    user_id = audit_user.id

    db_session.execute(delete(User).where(User.id == user_id))
    db_session.commit()
    db_session.refresh(log)
    assert log.user_id is None


def test_business_deletion_is_restricted(db_session: Session) -> None:
    scoped_business = business()
    db_session.add(
        ToolCallLog(
            business=scoped_business,
            tool_name="catalog.read",
            args_hash=VALID_DIGEST,
            status=ToolCallStatus.SUCCESS,
        )
    )
    db_session.commit()

    with pytest.raises(IntegrityError):
        db_session.execute(delete(Business).where(Business.id == scoped_business.id))
        db_session.commit()


def test_retention_deletes_only_strictly_older_rows(db_session: Session) -> None:
    current_time = datetime(2026, 8, 10, 12, tzinfo=UTC)
    cutoff = current_time - timedelta(days=90)
    scoped_business = business()
    old = ToolCallLog(
        business=scoped_business,
        tool_name="old.call",
        args_hash=VALID_DIGEST,
        status=ToolCallStatus.SUCCESS,
        created_at=cutoff - timedelta(microseconds=1),
    )
    at_cutoff = ToolCallLog(
        business=scoped_business,
        tool_name="cutoff.call",
        args_hash=VALID_DIGEST,
        status=ToolCallStatus.SUCCESS,
        created_at=cutoff,
    )
    recent = ToolCallLog(
        business=scoped_business,
        tool_name="recent.call",
        args_hash=VALID_DIGEST,
        status=ToolCallStatus.SUCCESS,
        created_at=current_time,
    )
    db_session.add_all([old, at_cutoff, recent])
    db_session.commit()

    assert (
        delete_expired_tool_call_logs(
            db_session, current_time=current_time, retention_days=90
        )
        == 1
    )
    remaining = set(db_session.scalars(select(ToolCallLog.tool_name)))
    assert remaining == {"cutoff.call", "recent.call"}


def test_audit_schema_contains_only_minimal_safe_fields(
    database_engine: Engine,
) -> None:
    columns = {
        column["name"]
        for column in inspect(database_engine).get_columns("tool_call_logs")
    }
    assert columns == {
        "id",
        "business_id",
        "user_id",
        "tool_name",
        "args_hash",
        "status",
        "error_code",
        "latency_ms",
        "created_at",
    }


def test_expected_audit_indexes_exist(database_engine: Engine) -> None:
    index_names = {
        index["name"]
        for index in inspect(database_engine).get_indexes("tool_call_logs")
    }
    assert {
        "ix_tool_logs_business_created",
        "ix_tool_logs_tool_status",
        "ix_tool_logs_non_success",
    } <= index_names
