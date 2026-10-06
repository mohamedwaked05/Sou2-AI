"""Controlled PostgreSQL-backed request admission limits."""

import uuid
from datetime import datetime

from fastapi import status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.exceptions import ApplicationError


def _rate_limited(
    *,
    code: str,
    message: str,
    retry_after_seconds: int,
    reset_at: datetime,
) -> ApplicationError:
    return ApplicationError(
        message,
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        error_code=code,
        details={"reset_at": reset_at.isoformat()},
        headers={"Retry-After": str(max(1, retry_after_seconds))},
    )


def admit_registration_attempt(
    session: Session, *, normalized_email: str, client_ip: str
) -> None:
    """Count one registration before expensive work through a protected function."""
    row = session.execute(
        text(
            "SELECT * FROM public.sou2ai_admit_registration_attempt("
            ":normalized_email, :client_ip)"
        ),
        {"normalized_email": normalized_email, "client_ip": client_ip},
    ).one()
    if not row.admitted:
        session.commit()
        raise _rate_limited(
            code=row.limit_code,
            message="Too many registration attempts. Try again later.",
            retry_after_seconds=row.retry_after_seconds,
            reset_at=row.reset_at,
        )
    # Admission must survive a later duplicate, password-policy, hashing, or
    # delivery failure because it already consumed resources.
    session.commit()


def admit_owner_chat_generation(
    session: Session,
    *,
    business_id: uuid.UUID,
    owner_message_id: uuid.UUID,
    generation_attempt: int,
    settings: Settings | None = None,
) -> None:
    """Serialize owner-generation admission across replicas in PostgreSQL."""
    development_override = (
        settings is not None
        and settings.environment.casefold() == "development"
        and (
            settings.development_owner_chat_minute_limit is not None
            or settings.development_owner_chat_hour_limit is not None
        )
    )
    function = (
        "sou2ai_admit_development_owner_chat_generation"
        if development_override
        else "sou2ai_admit_owner_chat_generation"
    )
    limit_parameters = ", :minute_limit, :hour_limit" if development_override else ""
    row = session.execute(
        text(
            f"SELECT * FROM public.{function}("
            f":business_id, :message_id, :generation_attempt{limit_parameters})"
        ),
        {
            "business_id": business_id,
            "message_id": owner_message_id,
            "generation_attempt": generation_attempt,
            "minute_limit": (settings.development_owner_chat_minute_limit or 3)
            if development_override
            else 3,
            "hour_limit": (settings.development_owner_chat_hour_limit or 20)
            if development_override
            else 20,
        },
    ).one()
    if not row.admitted:
        raise _rate_limited(
            code="owner_chat_rate_limited",
            message="Too many owner-chat generation attempts. Try again later.",
            retry_after_seconds=row.retry_after_seconds,
            reset_at=row.reset_at,
        )


def undo_owner_chat_generation_admission(
    session: Session,
    *,
    business_id: uuid.UUID,
    owner_message_id: uuid.UUID,
    generation_attempt: int,
    generation_claim_token: uuid.UUID,
) -> bool:
    """Undo only the current pre-provider admission after budget rejection."""
    undone = session.scalar(
        text(
            "SELECT public.sou2ai_undo_owner_chat_generation_admission("
            ":business_id, :message_id, :generation_attempt, :claim_token)"
        ),
        {
            "business_id": business_id,
            "message_id": owner_message_id,
            "generation_attempt": generation_attempt,
            "claim_token": generation_claim_token,
        },
    )
    session.commit()
    return bool(undone)
