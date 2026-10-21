"""Fail-closed admission for a coordinated activation window."""

from app.core.config import Settings, get_settings
from app.core.exceptions import ApplicationError


def require_open_admission(settings: Settings | None = None) -> None:
    """Reject producers before database claims, uploads, queues or provider use."""
    if (settings or get_settings()).admission_mode != "open":
        raise ApplicationError(
            "Service admission is closed.",
            status_code=503,
            error_code="admission_closed",
            headers={"Retry-After": "60"},
        )
