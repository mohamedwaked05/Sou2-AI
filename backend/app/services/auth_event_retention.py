"""Bounded, PostgreSQL-coordinated authentication-event retention."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, exists, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import AUTH_EVENT_MINIMUM_RETENTION_HOURS, Settings
from app.core.security import utc_now
from app.database.models import AuthenticationEvent
from app.database.session import get_session_factory

logger = logging.getLogger(__name__)

AUTH_EVENT_CLEANUP_BATCH_SIZE = 1_000
AUTH_EVENT_CLEANUP_LOCK_NAME = "sou2ai:authentication-event-cleanup"


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
