"""Persistent PostgreSQL-backed request admission limits."""

import math
import uuid
from datetime import datetime, timedelta

from fastapi import status
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.exceptions import ApplicationError
from app.database.models import (
    OwnerChatRateLimitEvent,
    RegistrationRateLimitEvent,
)

REGISTRATION_EMAIL_LIMIT = 5
REGISTRATION_EMAIL_WINDOW = timedelta(hours=1)
REGISTRATION_IP_SHORT_LIMIT = 30
REGISTRATION_IP_SHORT_WINDOW = timedelta(minutes=15)
REGISTRATION_IP_DAILY_LIMIT = 100
REGISTRATION_IP_DAILY_WINDOW = timedelta(hours=24)
OWNER_CHAT_MINUTE_LIMIT = 3
OWNER_CHAT_MINUTE_WINDOW = timedelta(minutes=1)
OWNER_CHAT_HOURLY_LIMIT = 20
OWNER_CHAT_HOURLY_WINDOW = timedelta(hours=1)


def _retry_seconds(now: datetime, reset_at: datetime) -> int:
    return max(1, math.ceil((reset_at - now).total_seconds()))


def _rate_limited(
    *, code: str, message: str, now: datetime, reset_at: datetime
) -> ApplicationError:
    retry_after = _retry_seconds(now, reset_at)
    return ApplicationError(
        message,
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        error_code=code,
        details={"reset_at": reset_at.isoformat()},
        headers={"Retry-After": str(retry_after)},
    )


def _database_now(session: Session) -> datetime:
    return session.scalar(select(func.clock_timestamp()))


def _lock_scopes(session: Session, scopes: tuple[str, ...]) -> None:
    for scope in sorted(scopes):
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
            {"scope": scope},
        )


def _oldest_registration_event(
    session: Session,
    *,
    since: datetime,
    normalized_email: str | None = None,
    client_ip: str | None = None,
) -> tuple[int, datetime | None]:
    filters = [RegistrationRateLimitEvent.created_at >= since]
    if normalized_email is not None:
        filters.append(RegistrationRateLimitEvent.normalized_email == normalized_email)
    if client_ip is not None:
        filters.append(RegistrationRateLimitEvent.client_ip == client_ip)
    row = session.execute(
        select(func.count(), func.min(RegistrationRateLimitEvent.created_at)).where(
            *filters
        )
    ).one()
    return int(row[0]), row[1]


def admit_registration_attempt(
    session: Session, *, normalized_email: str, client_ip: str
) -> None:
    """Atomically count an admitted registration before expensive work."""
    _lock_scopes(
        session,
        (
            f"registration:email:{normalized_email}",
            f"registration:ip:{client_ip}",
        ),
    )
    now = _database_now(session)
    checks = (
        (
            *_oldest_registration_event(
                session,
                since=now - REGISTRATION_EMAIL_WINDOW,
                normalized_email=normalized_email,
            ),
            REGISTRATION_EMAIL_LIMIT,
            REGISTRATION_EMAIL_WINDOW,
            "registration_email_rate_limited",
        ),
        (
            *_oldest_registration_event(
                session,
                since=now - REGISTRATION_IP_SHORT_WINDOW,
                client_ip=client_ip,
            ),
            REGISTRATION_IP_SHORT_LIMIT,
            REGISTRATION_IP_SHORT_WINDOW,
            "registration_ip_rate_limited",
        ),
        (
            *_oldest_registration_event(
                session,
                since=now - REGISTRATION_IP_DAILY_WINDOW,
                client_ip=client_ip,
            ),
            REGISTRATION_IP_DAILY_LIMIT,
            REGISTRATION_IP_DAILY_WINDOW,
            "registration_ip_daily_rate_limited",
        ),
    )
    for count, oldest, limit, window, code in checks:
        if count >= limit and oldest is not None:
            session.rollback()
            raise _rate_limited(
                code=code,
                message="Too many registration attempts. Try again later.",
                now=now,
                reset_at=oldest + window,
            )

    session.add(
        RegistrationRateLimitEvent(
            normalized_email=normalized_email,
            client_ip=client_ip,
        )
    )
    # The attempt must survive a later duplicate, password-policy, hashing, or
    # delivery failure because it already consumed admission resources.
    session.commit()


def _oldest_owner_event(
    session: Session, *, business_id: uuid.UUID, since: datetime
) -> tuple[int, datetime | None]:
    row = session.execute(
        select(func.count(), func.min(OwnerChatRateLimitEvent.created_at)).where(
            OwnerChatRateLimitEvent.business_id == business_id,
            OwnerChatRateLimitEvent.created_at >= since,
        )
    ).one()
    return int(row[0]), row[1]


def admit_owner_chat_generation(
    session: Session,
    *,
    business_id: uuid.UUID,
    owner_message_id: uuid.UUID,
    generation_attempt: int,
) -> None:
    """Serialize one business's owner-generation counters across replicas."""
    _lock_scopes(session, (f"owner-chat-generation:{business_id}",))
    existing = session.scalar(
        select(OwnerChatRateLimitEvent.id).where(
            OwnerChatRateLimitEvent.owner_message_id == owner_message_id,
            OwnerChatRateLimitEvent.generation_attempt == generation_attempt,
        )
    )
    if existing is not None:
        return

    now = _database_now(session)
    checks = (
        (
            *_oldest_owner_event(
                session,
                business_id=business_id,
                since=now - OWNER_CHAT_MINUTE_WINDOW,
            ),
            OWNER_CHAT_MINUTE_LIMIT,
            OWNER_CHAT_MINUTE_WINDOW,
        ),
        (
            *_oldest_owner_event(
                session,
                business_id=business_id,
                since=now - OWNER_CHAT_HOURLY_WINDOW,
            ),
            OWNER_CHAT_HOURLY_LIMIT,
            OWNER_CHAT_HOURLY_WINDOW,
        ),
    )
    for count, oldest, limit, window in checks:
        if count >= limit and oldest is not None:
            raise _rate_limited(
                code="owner_chat_rate_limited",
                message="Too many owner-chat generation attempts. Try again later.",
                now=now,
                reset_at=oldest + window,
            )
    session.add(
        OwnerChatRateLimitEvent(
            business_id=business_id,
            owner_message_id=owner_message_id,
            generation_attempt=generation_attempt,
        )
    )
