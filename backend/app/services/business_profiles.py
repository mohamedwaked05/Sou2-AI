"""Authoritative business-profile completion calculation."""

from dataclasses import dataclass

from app.database.models import Business, BusinessCategory, DefaultLanguage
from app.services.lebanese_locations import is_valid_location
from app.services.opening_hours import (
    DayInput,
    ScheduleValidationError,
    ShiftInput,
    validate_weekly_schedule,
)


@dataclass(frozen=True)
class ProfileIssue:
    field: str
    message: str
    section: str


def _text_issue(
    value: str | None,
    *,
    field: str,
    section: str,
    minimum: int,
    maximum: int,
) -> ProfileIssue | None:
    if value is None or not value.strip():
        return ProfileIssue(field, "This field is required.", section)
    length = len(value.strip())
    if length < minimum or length > maximum:
        return ProfileIssue(
            field,
            f"Must contain between {minimum} and {maximum} characters.",
            section,
        )
    return None


def business_profile_issues(business: Business) -> list[ProfileIssue]:
    """Return safe whole-profile validation issues in onboarding order."""
    issues: list[ProfileIssue] = []
    for value, field, minimum, maximum in (
        (business.name, "name", 2, 120),
        (business.description, "description", 20, 2000),
    ):
        issue = _text_issue(
            value,
            field=field,
            section="business_details",
            minimum=minimum,
            maximum=maximum,
        )
        if issue:
            issues.append(issue)

    if business.category is None:
        issues.append(
            ProfileIssue("category", "This field is required.", "business_details")
        )
    elif business.category is BusinessCategory.OTHER:
        issue = _text_issue(
            business.custom_category,
            field="custom_category",
            section="business_details",
            minimum=2,
            maximum=100,
        )
        if issue:
            issues.append(issue)
    elif business.custom_category is not None:
        issues.append(
            ProfileIssue(
                "custom_category",
                "Only the OTHER category may have a custom category.",
                "business_details",
            )
        )

    if business.default_language not in {DefaultLanguage.AR, DefaultLanguage.EN}:
        issues.append(
            ProfileIssue(
                "default_language", "This field is required.", "business_details"
            )
        )

    location_values = (
        (business.governorate, "governorate"),
        (business.district, "district"),
        (business.city, "city"),
    )
    for value, field in location_values:
        if value is None:
            issues.append(ProfileIssue(field, "This field is required.", "location"))
    if all(value is not None for value, _ in location_values) and not is_valid_location(
        business.governorate, business.district, business.city
    ):
        issues.append(
            ProfileIssue(
                "city",
                "The governorate, district, and city/area selection is invalid.",
                "location",
            )
        )
    address_issue = _text_issue(
        business.address_line,
        field="address_line",
        section="location",
        minimum=5,
        maximum=255,
    )
    if address_issue:
        issues.append(address_issue)

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
    except ScheduleValidationError as exc:
        issues.append(ProfileIssue("working_hours", str(exc), "working_hours"))
    return issues


def is_business_profile_complete(business: Business) -> bool:
    """Calculate completion solely from current persisted profile data."""
    return not business_profile_issues(business)


def first_incomplete_section(business: Business) -> str | None:
    """Identify where a resumable onboarding client should continue."""
    issues = business_profile_issues(business)
    return issues[0].section if issues else None
