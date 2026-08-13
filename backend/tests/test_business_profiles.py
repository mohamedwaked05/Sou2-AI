"""Profile-completion, activation-trigger, and atomic schedule tests."""

from datetime import time

import pytest
from app.core.security import utc_now
from app.database.models import (
    Business,
    BusinessCategory,
    BusinessOpeningDay,
    BusinessOpeningShift,
    BusinessStatus,
    User,
)
from app.services.business_profiles import is_business_profile_complete
from app.services.opening_hours import (
    DayInput,
    ScheduleValidationError,
    ShiftInput,
    replace_weekly_schedule,
)
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from tests.test_business_api import change_business_status


def complete_business(name: str = "Complete Shop") -> Business:
    owner = User(
        email=f"{name.lower().replace(' ', '-')}@example.com",
        first_name="Owner",
        last_name="Example",
        password_hash="hash",
    )
    return Business(
        owner=owner,
        name=name,
        description="A neighborhood grocery shop",
        category=BusinessCategory.GROCERY_SUPERMARKET,
        governorate="Beirut",
        district="Beirut",
        city="Beirut",
        address_line="Hamra Street, ground floor",
    )


def valid_week() -> tuple[DayInput, ...]:
    return (
        DayInput(0, True, (ShiftInput(time(9), time(18)),)),
        DayInput(1, True, (ShiftInput(time(10), time(16)),)),
    ) + tuple(DayInput(day, False) for day in range(2, 7))


def load_business(db_session: Session, business_id: object) -> Business:
    return db_session.scalar(
        select(Business)
        .where(Business.id == business_id)
        .options(
            selectinload(Business.opening_days).selectinload(BusinessOpeningDay.shifts)
        )
    )


def activate(db_session: Session, business: Business) -> None:
    business.onboarding_submitted_at = utc_now()
    db_session.commit()
    change_business_status(
        db_session,
        business.id,
        "ACTIVE",
        admin_identifier="test:profiles",
        reason="Profile guard test activation",
    )


def test_incomplete_business_is_allowed_while_disabled(db_session: Session) -> None:
    owner = User(
        email="draft@example.com",
        first_name="Draft",
        last_name="Owner",
        password_hash="hash",
    )
    business = Business(owner=owner, name="Draft")
    db_session.add(business)
    db_session.commit()

    assert business.status is BusinessStatus.PENDING
    assert not business.is_active
    assert not is_business_profile_complete(load_business(db_session, business.id))


@pytest.mark.parametrize(
    "missing_field",
    ["description", "category", "governorate", "district", "city", "address_line"],
)
def test_each_missing_profile_field_is_detected(
    db_session: Session, missing_field: str
) -> None:
    business = complete_business(missing_field)
    setattr(business, missing_field, None)
    db_session.add(business)
    db_session.commit()
    replace_weekly_schedule(db_session, business.id, valid_week())

    assert not is_business_profile_complete(load_business(db_session, business.id))


def test_missing_weekday_is_incomplete(db_session: Session) -> None:
    business = complete_business()
    business.opening_days = [
        BusinessOpeningDay(day_of_week=day, is_open=False) for day in range(6)
    ]
    db_session.add(business)
    db_session.commit()

    assert not is_business_profile_complete(load_business(db_session, business.id))


def test_invalid_open_and_closed_shift_configurations_are_incomplete(
    db_session: Session,
) -> None:
    business = complete_business()
    business.opening_days = [
        BusinessOpeningDay(day_of_week=day, is_open=day == 0) for day in range(7)
    ]
    business.opening_days[1].shifts = [
        BusinessOpeningShift(opens_at=time(9), closes_at=time(10))
    ]
    db_session.add(business)
    db_session.commit()

    assert not is_business_profile_complete(load_business(db_session, business.id))


def test_complete_profile_and_controlled_activation_succeed(
    db_session: Session,
) -> None:
    business = complete_business()
    db_session.add(business)
    db_session.commit()
    replace_weekly_schedule(db_session, business.id, valid_week())

    loaded = load_business(db_session, business.id)
    assert is_business_profile_complete(loaded)
    assert len(loaded.opening_days) == 7
    assert all(not day.shifts for day in loaded.opening_days if not day.is_open)
    activate(db_session, loaded)
    db_session.refresh(loaded)
    assert loaded.is_active


def test_controlled_activation_of_incomplete_business_is_rejected(
    db_session: Session,
) -> None:
    owner = User(
        email="incomplete@example.com",
        first_name="Incomplete",
        last_name="Owner",
        password_hash="hash",
    )
    business = Business(owner=owner, name="Incomplete")
    db_session.add(business)
    db_session.commit()

    business.onboarding_submitted_at = utc_now()
    db_session.commit()
    with pytest.raises(IntegrityError, match="complete confirmed profile"):
        change_business_status(
            db_session,
            business.id,
            "ACTIVE",
            admin_identifier="test:profiles",
            reason="Attempt incomplete activation",
        )


def test_failed_schedule_replacement_preserves_previous_schedule(
    db_session: Session,
) -> None:
    business = complete_business()
    db_session.add(business)
    db_session.commit()
    replace_weekly_schedule(db_session, business.id, valid_week())

    invalid = valid_week()[:-1]
    with pytest.raises(ScheduleValidationError):
        replace_weekly_schedule(db_session, business.id, invalid)

    day_count = db_session.scalar(
        select(func.count())
        .select_from(BusinessOpeningDay)
        .where(BusinessOpeningDay.business_id == business.id)
    )
    assert day_count == 7


def test_active_business_rejects_invalid_direct_schedule_edit(
    db_session: Session,
) -> None:
    business = complete_business()
    db_session.add(business)
    db_session.commit()
    replace_weekly_schedule(db_session, business.id, valid_week())
    business = load_business(db_session, business.id)
    activate(db_session, business)

    with pytest.raises(IntegrityError, match="retain a valid profile"):
        db_session.execute(
            text(
                "DELETE FROM business_opening_days "
                "WHERE business_id = :business_id AND day_of_week = 6"
            ),
            {"business_id": business.id},
        )
        db_session.commit()


@pytest.mark.parametrize(
    "column",
    ["description", "category", "governorate", "district", "city", "address_line"],
)
def test_active_business_rejects_incomplete_direct_profile_edit(
    db_session: Session, column: str
) -> None:
    business = complete_business(f"Guard {column}")
    db_session.add(business)
    db_session.commit()
    replace_weekly_schedule(db_session, business.id, valid_week())
    activate(db_session, business)

    with pytest.raises(IntegrityError, match="retain a valid profile"):
        db_session.execute(
            text(f"UPDATE businesses SET {column} = NULL WHERE id = :id"),
            {"id": business.id},
        )
        db_session.commit()
