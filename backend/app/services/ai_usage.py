"""Per-business local-day AI reservation and accounting services."""

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import status
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.agent.owner_chat_provider import TokenUsage
from app.core.exceptions import ApplicationError
from app.core.security import utc_now
from app.database.models import (
    Business,
    CustomerMessage,
    OwnerConversationSummary,
    User,
)
from app.schemas.ai_usage import CurrentAIUsageResponse
from app.services.businesses import load_full_access_business


@dataclass(frozen=True)
class AIUsageReservationClaim:
    id: uuid.UUID
    reset_at: datetime


def business_local_day_window(
    business: Business, *, moment: datetime | None = None
) -> tuple[datetime, datetime]:
    """Return the UTC instants bounding the business's current local day."""
    current = moment or utc_now()
    timezone = ZoneInfo(business.timezone)
    local_date = current.astimezone(timezone).date()
    start = datetime.combine(local_date, time.min, tzinfo=timezone)
    end = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=timezone)
    return start.astimezone(UTC), end.astimezone(UTC)


def _daily_limit_error(reset_at: datetime) -> ApplicationError:
    retry_after = max(1, math.ceil((reset_at - utc_now()).total_seconds()))
    return ApplicationError(
        "The daily AI allowance has been reached. Try again after it resets.",
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        error_code="daily_ai_token_limit_reached",
        details={"reset_at": reset_at.isoformat()},
        headers={"Retry-After": str(retry_after)},
    )


def reserve_owner_chat_usage(
    session: Session,
    *,
    business: Business,
    user: User,
    owner_message_id: uuid.UUID,
    generation_attempt: int,
    estimated_input_tokens: int,
    max_output_tokens: int,
    lease_seconds: int,
) -> AIUsageReservationClaim:
    window_start, window_end = business_local_day_window(business)
    try:
        row = session.execute(
            text(
                "SELECT * FROM public.sou2ai_reserve_ai_usage("
                ":business_id, :user_id, :message_id, :attempt, 'owner', "
                "'owner_chat', :estimated_input, :max_output, :lease_seconds)"
            ),
            {
                "business_id": business.id,
                "user_id": user.id,
                "message_id": owner_message_id,
                "attempt": generation_attempt,
                "estimated_input": estimated_input_tokens,
                "max_output": max_output_tokens,
                "lease_seconds": lease_seconds,
            },
        ).one()
        session.commit()
    except DBAPIError as exc:
        session.rollback()
        if "daily_ai_token_limit_reached" in str(exc.orig):
            raise _daily_limit_error(window_end) from None
        raise
    return AIUsageReservationClaim(id=row.reservation_id, reset_at=row.reset_at)


def reserve_conversation_summary_usage(
    session: Session,
    *,
    summary: OwnerConversationSummary,
    claim_token: uuid.UUID,
    estimated_input_tokens: int,
    max_output_tokens: int,
    lease_seconds: int,
) -> AIUsageReservationClaim:
    business = session.get(Business, summary.business_id)
    if business is None:
        raise RuntimeError("Summary business is unavailable.")
    _, window_end = business_local_day_window(business)
    try:
        row = session.execute(
            text(
                "SELECT * FROM public.sou2ai_reserve_conversation_summary_usage("
                ":summary_id, :claim_token, :estimated_input, :max_output, "
                ":lease_seconds)"
            ),
            {
                "summary_id": summary.id,
                "claim_token": claim_token,
                "estimated_input": estimated_input_tokens,
                "max_output": max_output_tokens,
                "lease_seconds": lease_seconds,
            },
        ).one()
        session.commit()
    except DBAPIError as exc:
        session.rollback()
        if "daily_ai_token_limit_reached" in str(exc.orig):
            raise _daily_limit_error(window_end) from None
        raise
    return AIUsageReservationClaim(id=row.reservation_id, reset_at=row.reset_at)


def reserve_customer_message_usage(
    session: Session,
    *,
    message: CustomerMessage,
    estimated_input_tokens: int,
    max_output_tokens: int,
    lease_seconds: int,
) -> AIUsageReservationClaim:
    business = session.get(Business, message.business_id)
    if business is None:
        raise RuntimeError("Customer message business is unavailable.")
    _, window_end = business_local_day_window(business)
    try:
        row = session.execute(
            text(
                "SELECT * FROM public.sou2ai_reserve_customer_message_usage("
                ":message_id, :estimated_input, :max_output, :lease_seconds)"
            ),
            {
                "message_id": message.id,
                "estimated_input": estimated_input_tokens,
                "max_output": max_output_tokens,
                "lease_seconds": lease_seconds,
            },
        ).one()
        session.commit()
    except DBAPIError as exc:
        session.rollback()
        if "daily_ai_token_limit_reached" in str(exc.orig):
            raise _daily_limit_error(window_end) from None
        raise
    return AIUsageReservationClaim(id=row.reservation_id, reset_at=row.reset_at)


def reconcile_ai_usage(
    session: Session,
    reservation_id: uuid.UUID,
    *,
    usage: TokenUsage | None,
    outcome: str,
    provider_identifier: str | None = None,
    model_identifier: str | None = None,
    commit: bool = True,
) -> bool:
    """Finalize one leased reservation exactly once."""
    row = session.execute(
        text(
            "SELECT * FROM public.sou2ai_reconcile_ai_usage("
            ":reservation_id, :input_tokens, :output_tokens, :authoritative, "
            ":provider, :model, :outcome)"
        ),
        {
            "reservation_id": reservation_id,
            "input_tokens": usage.input_tokens if usage is not None else None,
            "output_tokens": usage.output_tokens if usage is not None else None,
            "authoritative": usage.authoritative if usage is not None else False,
            "provider": provider_identifier,
            "model": model_identifier,
            "outcome": outcome,
        },
    ).one()
    if commit:
        session.commit()
    return bool(row.reconciled)


def _usage_status(percentage: float) -> str:
    if percentage >= 100:
        return "exhausted"
    if percentage >= 90:
        return "nearly_exhausted"
    if percentage >= 75:
        return "approaching_limit"
    return "normal"


def get_current_ai_usage(
    session: Session, user: User, business_id: uuid.UUID
) -> CurrentAIUsageResponse:
    business = load_full_access_business(session, user, business_id)
    row = session.execute(
        text("SELECT * FROM public.sou2ai_get_current_ai_usage(:business_id)"),
        {
            "business_id": business.id,
        },
    ).one()
    session.commit()
    allowance = row.daily_token_allowance
    reserved_for_owner = allowance * row.owner_reserve_percent // 100
    remaining = max(0, allowance - row.total_tokens_used - row.tokens_reserved)
    availability_used = row.total_tokens_used + row.tokens_reserved
    percentage = round((availability_used / allowance) * 100, 2)
    timezone = ZoneInfo(business.timezone)
    local_window_start = row.usage_window_start.astimezone(timezone)
    local_window_end = row.usage_window_end.astimezone(timezone)
    return CurrentAIUsageResponse(
        window_start=local_window_start,
        window_end=local_window_end,
        reset_at=local_window_end,
        daily_token_allowance=allowance,
        owner_reserved_tokens=reserved_for_owner,
        input_tokens_used=row.input_tokens_used,
        output_tokens_used=row.output_tokens_used,
        total_tokens_used=row.total_tokens_used,
        tokens_currently_reserved=row.tokens_reserved,
        tokens_remaining=remaining,
        usage_percentage=percentage,
        status=_usage_status(percentage),
    )
