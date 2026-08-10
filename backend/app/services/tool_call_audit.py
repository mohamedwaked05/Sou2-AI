"""Retention operation for privacy-minimal tool-call metadata."""

from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.database.models import ToolCallLog


def delete_expired_tool_call_logs(
    session: Session, *, current_time: datetime, retention_days: int
) -> int:
    """Delete rows strictly older than the caller-provided UTC cutoff."""
    if current_time.tzinfo is None:
        raise ValueError("current_time must be timezone-aware.")
    if retention_days < 1:
        raise ValueError("retention_days must be positive.")
    cutoff = current_time - timedelta(days=retention_days)
    result = session.execute(delete(ToolCallLog).where(ToolCallLog.created_at < cutoff))
    return result.rowcount
