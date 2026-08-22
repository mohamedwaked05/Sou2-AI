"""Request and response schemas for business onboarding."""

import uuid
from datetime import datetime, time
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator

from app.database.models import BusinessCategory, BusinessStatus, DefaultLanguage
from app.services.lebanese_locations import LOCATION_HIERARCHY


class Weekday(StrEnum):
    MONDAY = "MONDAY"
    TUESDAY = "TUESDAY"
    WEDNESDAY = "WEDNESDAY"
    THURSDAY = "THURSDAY"
    FRIDAY = "FRIDAY"
    SATURDAY = "SATURDAY"
    SUNDAY = "SUNDAY"


WEEKDAY_TO_NUMBER = {weekday: index for index, weekday in enumerate(Weekday)}
NUMBER_TO_WEEKDAY = {value: key for key, value in WEEKDAY_TO_NUMBER.items()}


def _trim_limited(value: str, minimum: int, maximum: int) -> str:
    clean = value.strip()
    if not minimum <= len(clean) <= maximum:
        raise ValueError(f"Must contain between {minimum} and {maximum} characters.")
    return clean


class BusinessCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _trim_limited(" ".join(value.split()), 2, 120)


class ShiftInputSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: time
    end: time


class WorkingDayInputSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weekday: Weekday
    is_closed: bool
    shifts: list[ShiftInputSchema]


class BusinessUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    category: BusinessCategory | None = None
    custom_category: str | None = None
    default_language: DefaultLanguage | None = None
    governorate: str | None = None
    district: str | None = None
    city: str | None = None
    address_line: str | None = None
    working_hours: list[WorkingDayInputSchema] | None = None

    @field_validator("name")
    @classmethod
    def validate_optional_name(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("Business name cannot be cleared.")
        return _trim_limited(" ".join(value.split()), 2, 120)

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str | None) -> str | None:
        return None if value is None else _trim_limited(value, 20, 2000)

    @field_validator("custom_category")
    @classmethod
    def validate_custom_category(cls, value: str | None) -> str | None:
        return None if value is None else _trim_limited(value, 2, 100)

    @field_validator("address_line")
    @classmethod
    def validate_address(cls, value: str | None) -> str | None:
        return None if value is None else _trim_limited(value, 5, 255)

    @field_validator("governorate")
    @classmethod
    def validate_governorate(cls, value: str | None) -> str | None:
        if value is not None and value not in LOCATION_HIERARCHY:
            raise ValueError("Unknown Lebanese governorate.")
        return value

    @field_validator("district")
    @classmethod
    def validate_district(cls, value: str | None) -> str | None:
        districts = {item for values in LOCATION_HIERARCHY.values() for item in values}
        if value is not None and value not in districts:
            raise ValueError("Unknown Lebanese district.")
        return value

    @field_validator("city")
    @classmethod
    def validate_city(cls, value: str | None) -> str | None:
        cities = {
            item
            for districts in LOCATION_HIERARCHY.values()
            for values in districts.values()
            for item in values
        }
        if value is not None and value not in cities:
            raise ValueError("Unknown Lebanese city/area.")
        return value


class ShiftResponse(BaseModel):
    start: time
    end: time


class WorkingDayResponse(BaseModel):
    weekday: Weekday
    is_closed: bool
    shifts: list[ShiftResponse]


class BusinessResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    category: BusinessCategory | None
    custom_category: str | None
    default_language: DefaultLanguage | None
    governorate: str | None
    district: str | None
    city: str | None
    address_line: str | None
    status: BusinessStatus
    is_active: bool
    profile_complete: bool
    first_incomplete_section: str | None
    onboarding_submitted_at: datetime | None
    working_hours: list[WorkingDayResponse]
    created_at: datetime
    updated_at: datetime
