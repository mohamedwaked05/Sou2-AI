"""Validation, normalization, and atomic replacement of weekly schedules."""

import uuid
from dataclasses import dataclass
from datetime import time

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.database.models import BusinessOpeningDay, BusinessOpeningShift


class ScheduleValidationError(ValueError):
    """A proposed weekly schedule violates a domain invariant."""


@dataclass(frozen=True)
class ShiftInput:
    opens_at: time
    closes_at: time


@dataclass(frozen=True)
class DayInput:
    day_of_week: int
    is_open: bool
    shifts: tuple[ShiftInput, ...] = ()


def _to_minutes(value: time) -> int:
    if value.second or value.microsecond:
        raise ScheduleValidationError("Opening hours must use whole minutes.")
    return value.hour * 60 + value.minute


def normalize_shifts(shifts: tuple[ShiftInput, ...]) -> tuple[ShiftInput, ...]:
    """Reject invalid intervals and return chronological, separate shifts."""
    if len(shifts) > 3:
        raise ScheduleValidationError("An open day may have at most three shifts.")

    intervals: list[tuple[int, int]] = []
    for shift in shifts:
        start = _to_minutes(shift.opens_at)
        end = _to_minutes(shift.closes_at)
        if start >= end:
            raise ScheduleValidationError(
                "A shift start must be strictly before its end."
            )
        intervals.append((start, end))

    intervals.sort(key=lambda item: item[0])
    for previous, current in zip(intervals, intervals[1:], strict=False):
        if current[0] < previous[1]:
            raise ScheduleValidationError("Opening shifts cannot overlap.")
    return tuple(
        ShiftInput(time(start // 60, start % 60), time(end // 60, end % 60))
        for start, end in intervals
    )


def validate_weekly_schedule(days: tuple[DayInput, ...]) -> tuple[DayInput, ...]:
    """Validate all seven days before any persistence work begins."""
    if len(days) != 7 or {day.day_of_week for day in days} != set(range(7)):
        raise ScheduleValidationError(
            "A weekly schedule must contain Monday through Sunday."
        )

    normalized_days: list[DayInput] = []
    for day in sorted(days, key=lambda item: item.day_of_week):
        if not day.is_open and day.shifts:
            raise ScheduleValidationError("A closed day cannot contain shifts.")
        if day.is_open and not day.shifts:
            raise ScheduleValidationError("An open day requires at least one shift.")
        shifts = normalize_shifts(day.shifts) if day.is_open else ()
        normalized_days.append(DayInput(day.day_of_week, day.is_open, shifts))
    return tuple(normalized_days)


def replace_weekly_schedule(
    session: Session, business_id: uuid.UUID, days: tuple[DayInput, ...]
) -> tuple[DayInput, ...]:
    """Replace a complete schedule in one transaction after full validation."""
    normalized_days = validate_weekly_schedule(days)
    session.execute(
        delete(BusinessOpeningDay).where(BusinessOpeningDay.business_id == business_id)
    )
    for proposed_day in normalized_days:
        day = BusinessOpeningDay(
            business_id=business_id,
            day_of_week=proposed_day.day_of_week,
            is_open=proposed_day.is_open,
        )
        day.shifts = [
            BusinessOpeningShift(opens_at=shift.opens_at, closes_at=shift.closes_at)
            for shift in proposed_day.shifts
        ]
        session.add(day)
    session.flush()
    return normalized_days
