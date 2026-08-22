"""Transactional business management and tenant authorization."""

import uuid

from fastapi import status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.exceptions import ApplicationError
from app.core.security import utc_now
from app.database.models import (
    Business,
    BusinessCategory,
    BusinessMembership,
    BusinessOpeningDay,
    BusinessStatus,
    MembershipPermission,
    OwnerConversation,
    User,
)
from app.schemas.business import (
    NUMBER_TO_WEEKDAY,
    WEEKDAY_TO_NUMBER,
    BusinessResponse,
    BusinessUpdateRequest,
    ShiftResponse,
    WorkingDayResponse,
)
from app.services.business_profiles import (
    business_profile_issues,
    first_incomplete_section,
    is_business_profile_complete,
)
from app.services.lebanese_locations import is_valid_location
from app.services.opening_hours import (
    DayInput,
    ScheduleValidationError,
    ShiftInput,
    replace_weekly_schedule,
)


def _business_query(user_id: uuid.UUID, *, full_access: bool = False):
    query = (
        select(Business)
        .join(BusinessMembership)
        .where(
            BusinessMembership.user_id == user_id,
            BusinessMembership.business_id == Business.id,
        )
        .options(
            selectinload(Business.opening_days).selectinload(BusinessOpeningDay.shifts)
        )
    )
    if full_access:
        query = query.where(
            BusinessMembership.permission == MembershipPermission.FULL_ACCESS
        )
    return query


def _not_found() -> ApplicationError:
    return ApplicationError(
        "Business was not found.",
        status_code=status.HTTP_404_NOT_FOUND,
        error_code="business_not_found",
    )


def _conflict() -> ApplicationError:
    return ApplicationError(
        "A business with this name already exists for this owner.",
        status_code=status.HTTP_409_CONFLICT,
        error_code="business_name_conflict",
    )


def _response(business: Business) -> BusinessResponse:
    days = sorted(business.opening_days, key=lambda item: item.day_of_week)
    return BusinessResponse(
        id=business.id,
        name=business.name,
        description=business.description,
        category=business.category,
        custom_category=business.custom_category,
        default_language=business.default_language,
        governorate=business.governorate,
        district=business.district,
        city=business.city,
        address_line=business.address_line,
        status=business.status,
        is_active=business.status is BusinessStatus.ACTIVE,
        profile_complete=is_business_profile_complete(business),
        first_incomplete_section=first_incomplete_section(business),
        onboarding_submitted_at=business.onboarding_submitted_at,
        working_hours=[
            WorkingDayResponse(
                weekday=NUMBER_TO_WEEKDAY[day.day_of_week],
                is_closed=not day.is_open,
                shifts=[
                    ShiftResponse(start=shift.opens_at, end=shift.closes_at)
                    for shift in sorted(day.shifts, key=lambda item: item.opens_at)
                ],
            )
            for day in days
        ],
        created_at=business.created_at,
        updated_at=business.updated_at,
    )


def create_business(session: Session, user: User, name: str) -> BusinessResponse:
    """Create a pending business and creator membership in one transaction."""
    business = Business(owner_user_id=user.id, name=name)
    membership = BusinessMembership(
        user_id=user.id,
        business=business,
        permission=MembershipPermission.FULL_ACCESS,
    )
    conversation = OwnerConversation(business=business)
    session.add_all([business, membership, conversation])
    try:
        session.flush()
        session.commit()
    except IntegrityError:
        session.rollback()
        raise _conflict() from None
    return _response(business)


def list_businesses(session: Session, user: User) -> list[BusinessResponse]:
    businesses = session.scalars(
        _business_query(user.id).order_by(Business.created_at, Business.id)
    ).all()
    return [_response(business) for business in businesses]


def get_business(
    session: Session, user: User, business_id: uuid.UUID
) -> BusinessResponse:
    business = session.scalar(
        _business_query(user.id).where(Business.id == business_id)
    )
    if business is None:
        raise _not_found()
    return _response(business)


def _load_for_update(session: Session, user: User, business_id: uuid.UUID) -> Business:
    business = session.scalar(
        _business_query(user.id, full_access=True)
        .where(Business.id == business_id)
        .with_for_update(of=Business)
    )
    if business is None:
        raise _not_found()
    return business


def load_full_access_business(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> Business:
    """Resolve a tenant only through its full-access membership."""
    query = _business_query(user.id, full_access=True).where(Business.id == business_id)
    if for_update:
        query = query.with_for_update(of=Business)
    business = session.scalar(query)
    if business is None:
        raise _not_found()
    return business


def _validate_category(business: Business) -> None:
    if business.category is BusinessCategory.OTHER and business.custom_category is None:
        raise ApplicationError(
            "A custom category is required when category is OTHER.",
            status_code=422,
            error_code="invalid_business_category",
        )
    if (
        business.category is not BusinessCategory.OTHER
        and business.custom_category is not None
    ):
        raise ApplicationError(
            "A predefined category cannot include a custom category.",
            status_code=422,
            error_code="invalid_business_category",
        )


def _validate_location(business: Business) -> None:
    values = (business.governorate, business.district, business.city)
    if all(value is not None for value in values) and not is_valid_location(*values):
        raise ApplicationError(
            "The governorate, district, and city/area selection is invalid.",
            status_code=422,
            error_code="invalid_business_location",
        )


def update_business(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    body: BusinessUpdateRequest,
) -> BusinessResponse:
    """Apply one profile/schedule patch atomically with row-level serialization."""
    business = _load_for_update(session, user, business_id)
    changes = body.model_dump(exclude_unset=True, exclude={"working_hours"})
    if "category" in changes and changes["category"] is not BusinessCategory.OTHER:
        if changes.get("custom_category") is not None:
            session.rollback()
            raise ApplicationError(
                "A predefined category cannot include a custom category.",
                status_code=422,
                error_code="invalid_business_category",
            )
        changes["custom_category"] = None
    for field, value in changes.items():
        setattr(business, field, value)

    try:
        _validate_category(business)
        _validate_location(business)
        if body.working_hours is not None:
            proposed_days = tuple(
                DayInput(
                    day_of_week=WEEKDAY_TO_NUMBER[day.weekday],
                    is_open=not day.is_closed,
                    shifts=tuple(
                        ShiftInput(opens_at=shift.start, closes_at=shift.end)
                        for shift in day.shifts
                    ),
                )
                for day in body.working_hours
            )
            replace_weekly_schedule(session, business.id, proposed_days)
        session.flush()
        session.commit()
    except (ScheduleValidationError, IntegrityError) as exc:
        session.rollback()
        if isinstance(exc, IntegrityError):
            if "active business must retain a valid profile" in str(exc).casefold():
                raise ApplicationError(
                    "An active business must retain a complete profile.",
                    status_code=422,
                    error_code="business_profile_incomplete",
                ) from None
            raise _conflict() from None
        raise ApplicationError(
            str(exc), status_code=422, error_code="invalid_working_hours"
        ) from None
    except ApplicationError:
        session.rollback()
        raise

    refreshed = session.scalar(
        _business_query(user.id)
        .where(Business.id == business_id)
        .execution_options(populate_existing=True)
    )
    if refreshed is None:  # pragma: no cover - protected by the transaction above
        raise _not_found()
    return _response(refreshed)


def confirm_onboarding(
    session: Session, user: User, business_id: uuid.UUID
) -> BusinessResponse:
    """Record the first successful whole-profile confirmation without activation."""
    business = _load_for_update(session, user, business_id)
    issues = business_profile_issues(business)
    if issues:
        session.rollback()
        raise ApplicationError(
            "Business onboarding is incomplete.",
            status_code=422,
            error_code="business_profile_incomplete",
            details={
                "first_incomplete_section": issues[0].section,
                "fields": [
                    {"field": issue.field, "message": issue.message} for issue in issues
                ],
            },
        )
    if business.onboarding_submitted_at is None:
        business.onboarding_submitted_at = utc_now()
    session.commit()
    return _response(business)
