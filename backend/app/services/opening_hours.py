"""Validation, normalization, and atomic replacement of weekly schedules."""

import uuid
from dataclasses import dataclass
from datetime import time

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.database.models import BusinessOpeningDay, BusinessOpeningShift

MINUTES_PER_DAY = 24 * 60


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


def _to_time(value: int) -> time:
    minute = value % MINUTES_PER_DAY
    return time(hour=minute // 60, minute=minute % 60)


def normalize_shifts(shifts: tuple[ShiftInput, ...]) -> tuple[ShiftInput, ...]:
    """Reject circular overlaps and merge intervals that exactly touch."""
    intervals: list[tuple[int, int]] = []
    for shift in shifts:
        start = _to_minutes(shift.opens_at)
        end = _to_minutes(shift.closes_at)
        if start == end:
            raise ScheduleValidationError("Opening and closing times must differ.")
        if end < start:
            end += MINUTES_PER_DAY
        intervals.append((start, end))

    changed = True
    while changed:
        changed = False
        for left_index, left in enumerate(intervals):
            for right_index in range(left_index + 1, len(intervals)):
                right = intervals[right_index]
                for offset in (-MINUTES_PER_DAY, 0, MINUTES_PER_DAY):
                    shifted = (right[0] + offset, right[1] + offset)
                    if max(left[0], shifted[0]) < min(left[1], shifted[1]):
                        raise ScheduleValidationError("Opening shifts cannot overlap.")
                    if left[1] == shifted[0] or shifted[1] == left[0]:
                        merged = (min(left[0], shifted[0]), max(left[1], shifted[1]))
                        if merged[1] - merged[0] >= MINUTES_PER_DAY:
                            raise ScheduleValidationError(
                                "A merged shift cannot cover a full day."
                            )
                        canonical_start = merged[0] % MINUTES_PER_DAY
                        canonical_end = canonical_start + (merged[1] - merged[0])
                        intervals[left_index] = (canonical_start, canonical_end)
                        intervals.pop(right_index)
                        changed = True
                        break
                if changed:
                    break
            if changed:
                break

    if len(intervals) > 3:
        raise ScheduleValidationError("An open day may have at most three shifts.")

    intervals.sort(key=lambda item: item[0])
    return tuple(ShiftInput(_to_time(start), _to_time(end)) for start, end in intervals)


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
    with session.begin():
        session.execute(
            delete(BusinessOpeningDay).where(
                BusinessOpeningDay.business_id == business_id
            )
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
    return normalized_days
