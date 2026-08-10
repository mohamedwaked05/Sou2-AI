"""Pure domain tests for circular weekly opening-hour normalization."""

from datetime import time

import pytest
from app.services.opening_hours import (
    DayInput,
    ScheduleValidationError,
    ShiftInput,
    normalize_shifts,
    validate_weekly_schedule,
)


def shift(opens: tuple[int, int], closes: tuple[int, int]) -> ShiftInput:
    return ShiftInput(time(*opens), time(*closes))


def complete_week(monday: DayInput | None = None) -> tuple[DayInput, ...]:
    return (monday or DayInput(0, False),) + tuple(
        DayInput(day, False) for day in range(1, 7)
    )


def test_all_seven_days_and_closed_days_are_valid() -> None:
    assert len(validate_weekly_schedule(complete_week())) == 7


def test_open_day_requires_a_shift() -> None:
    with pytest.raises(ScheduleValidationError, match="requires"):
        validate_weekly_schedule(complete_week(DayInput(0, True)))


def test_closed_day_rejects_shifts() -> None:
    with pytest.raises(ScheduleValidationError, match="closed"):
        validate_weekly_schedule(
            complete_week(DayInput(0, False, (shift((9, 0), (10, 0)),)))
        )


def test_equal_times_are_rejected() -> None:
    with pytest.raises(ScheduleValidationError, match="differ"):
        normalize_shifts((shift((9, 0), (9, 0)),))


def test_overnight_shift_is_accepted() -> None:
    assert normalize_shifts((shift((20, 0), (2, 0)),)) == (shift((20, 0), (2, 0)),)


def test_overlapping_shifts_are_rejected() -> None:
    with pytest.raises(ScheduleValidationError, match="overlap"):
        normalize_shifts((shift((9, 0), (14, 0)), shift((13, 0), (18, 0))))


def test_touching_shifts_merge() -> None:
    assert normalize_shifts((shift((9, 0), (13, 0)), shift((13, 0), (18, 0)))) == (
        shift((9, 0), (18, 0)),
    )


def test_touching_chain_merges() -> None:
    assert normalize_shifts(
        (
            shift((13, 0), (18, 0)),
            shift((9, 0), (11, 0)),
            shift((11, 0), (13, 0)),
        )
    ) == (shift((9, 0), (18, 0)),)


def test_overnight_overlap_is_rejected() -> None:
    with pytest.raises(ScheduleValidationError, match="overlap"):
        normalize_shifts((shift((20, 0), (2, 0)), shift((1, 0), (3, 0))))


def test_more_than_three_final_shifts_is_rejected() -> None:
    with pytest.raises(ScheduleValidationError, match="three"):
        normalize_shifts(
            (
                shift((1, 0), (2, 0)),
                shift((4, 0), (5, 0)),
                shift((7, 0), (8, 0)),
                shift((10, 0), (11, 0)),
            )
        )


def test_schedule_requires_each_weekday_exactly_once() -> None:
    with pytest.raises(ScheduleValidationError, match="Monday"):
        validate_weekly_schedule(tuple(DayInput(day, False) for day in range(6)))
