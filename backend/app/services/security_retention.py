"""PostgreSQL-coordinated best-effort security-record retention."""

import logging
import uuid
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.core.security import utc_now
from app.database.models import AuthenticationMaintenanceTask
from app.database.session import get_session_factory

logger = logging.getLogger(__name__)
SECURITY_RETENTION_LOCK = "sou2ai:security-record-retention"
SECURITY_RETENTION_TASK = "security-record-retention"
SECURITY_RETENTION_BATCH_SIZE = 1_000


def cleanup_security_records_best_effort(settings: Settings) -> None:
    """Claim one shared maintenance interval and clean bounded record batches."""
    try:
        with get_session_factory()() as session:
            now = utc_now()
            acquired = session.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:name, 0))"),
                {"name": SECURITY_RETENTION_LOCK},
            )
            if not acquired:
                session.rollback()
                return
            next_run = now + timedelta(
                minutes=settings.security_event_cleanup_interval_minutes
            )
            claimed = session.scalar(
                insert(AuthenticationMaintenanceTask)
                .values(
                    id=uuid.uuid4(),
                    task_name=SECURITY_RETENTION_TASK,
                    next_run_at=next_run,
                )
                .on_conflict_do_update(
                    index_elements=[AuthenticationMaintenanceTask.task_name],
                    set_={"next_run_at": next_run},
                    where=AuthenticationMaintenanceTask.next_run_at <= now,
                )
                .returning(AuthenticationMaintenanceTask.id)
            )
            if claimed is None:
                session.rollback()
                return
            session.commit()
            session.execute(
                text(
                    "SELECT * FROM public.sou2ai_cleanup_security_records("
                    ":now, :batch_size)"
                ),
                {"now": utc_now(), "batch_size": SECURITY_RETENTION_BATCH_SIZE},
            )
            session.commit()
    except SQLAlchemyError:
        logger.warning("Security-record cleanup failed.")
