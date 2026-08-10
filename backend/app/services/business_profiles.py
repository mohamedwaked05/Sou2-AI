"""Authoritative business-profile completion calculation."""

from app.database.models import Business, DefaultLanguage
from app.services.opening_hours import (
    DayInput,
    ScheduleValidationError,
    ShiftInput,
    validate_weekly_schedule,
)


def _has_text(value: str | None) -> bool:
    return bool(value and value.strip())


def is_business_profile_complete(business: Business) -> bool:
    """Calculate completion solely from current profile and schedule data."""
    required_text = (
        business.description,
        business.industry,
        business.governorate,
        business.city,
        business.address_line,
    )
    if not all(_has_text(value) for value in required_text):
        return False
    if business.default_language not in (DefaultLanguage.AR, DefaultLanguage.EN):
        return False

    days = tuple(
        DayInput(
            day_of_week=day.day_of_week,
            is_open=day.is_open,
            shifts=tuple(
                ShiftInput(opens_at=shift.opens_at, closes_at=shift.closes_at)
                for shift in day.shifts
            ),
        )
        for day in business.opening_days
    )
    try:
        validate_weekly_schedule(days)
    except ScheduleValidationError:
        return False
    return True
