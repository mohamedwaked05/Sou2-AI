"""Bounded, PostgreSQL-coordinated authentication-event retention."""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, exists, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import AUTH_EVENT_MINIMUM_RETENTION_HOURS, Settings
from app.core.security import utc_now
from app.database.models import AuthenticationEvent, AuthenticationMaintenanceTask
from app.database.session import get_session_factory

logger = logging.getLogger(__name__)

AUTH_EVENT_CLEANUP_BATCH_SIZE = 1_000
AUTH_EVENT_CLEANUP_LOCK_NAME = "sou2ai:authentication-event-cleanup"
AUTH_EVENT_CLEANUP_TASK_NAME = "authentication-event-retention"


def claim_authentication_event_cleanup(
    session: Session,
    *,
    current_time: datetime,
    interval_minutes: int,
) -> bool:
    """Atomically claim a due cleanup interval without waiting for another worker."""
    if current_time.tzinfo is None:
        raise ValueError("current_time must be timezone-aware.")
    if interval_minutes < 1:
        raise ValueError("interval_minutes must be positive.")

    acquired = session.scalar(
        text("SELECT pg_try_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
        {"lock_name": AUTH_EVENT_CLEANUP_LOCK_NAME},
    )
    if not acquired:
        return False

    claimed_at = current_time.astimezone(UTC)
    next_run_at = claimed_at + timedelta(minutes=interval_minutes)
    claim = (
        insert(AuthenticationMaintenanceTask)
        .values(
            id=uuid.uuid4(),
            task_name=AUTH_EVENT_CLEANUP_TASK_NAME,
            next_run_at=next_run_at,
        )
        .on_conflict_do_update(
            index_elements=[AuthenticationMaintenanceTask.task_name],
            set_={"next_run_at": next_run_at},
            where=AuthenticationMaintenanceTask.next_run_at <= claimed_at,
        )
        .returning(AuthenticationMaintenanceTask.id)
    )
    return session.scalar(claim) is not None


def delete_expired_authentication_events(
    session: Session,
    *,
    current_time: datetime,
    retention_hours: int,
    batch_size: int = AUTH_EVENT_CLEANUP_BATCH_SIZE,
) -> int:
    """Delete one bounded batch of events strictly older than the UTC cutoff."""
    if current_time.tzinfo is None:
        raise ValueError("current_time must be timezone-aware.")
    if retention_hours < AUTH_EVENT_MINIMUM_RETENTION_HOURS:
        raise ValueError("retention_hours must be at least 2.")
    if batch_size < 1:
        raise ValueError("batch_size must be positive.")

    cutoff = current_time.astimezone(UTC) - timedelta(hours=retention_hours)
    has_expired_events = session.scalar(
        select(exists().where(AuthenticationEvent.created_at < cutoff))
    )
    if not has_expired_events:
        return 0

    acquired = session.scalar(
        text("SELECT pg_try_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
        {"lock_name": AUTH_EVENT_CLEANUP_LOCK_NAME},
    )
    if not acquired:
        return 0

    expired_ids = (
        select(AuthenticationEvent.id)
        .where(AuthenticationEvent.created_at < cutoff)
        .order_by(AuthenticationEvent.created_at, AuthenticationEvent.id)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    result = session.execute(
        delete(AuthenticationEvent).where(AuthenticationEvent.id.in_(expired_ids))
    )
    return result.rowcount or 0


def cleanup_authentication_events_best_effort(settings: Settings) -> int:
    """Run isolated maintenance without changing an authentication response."""
    try:
        with get_session_factory()() as session:
            claimed = claim_authentication_event_cleanup(
                session,
                current_time=utc_now(),
                interval_minutes=settings.auth_event_cleanup_interval_minutes,
            )
            if not claimed:
                session.rollback()
                return 0

            session.commit()
            deleted_count = delete_expired_authentication_events(
                session,
                current_time=utc_now(),
                retention_hours=settings.auth_event_retention_hours,
            )
            session.commit()
            return deleted_count
    except SQLAlchemyError:
        logger.warning("Authentication-event cleanup failed.")
        return 0
