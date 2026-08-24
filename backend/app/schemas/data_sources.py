"""Safe public schemas for tenant operational data source management."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.database.models import OperationalDataSourceStatus


class MappingProfileResponse(BaseModel):
    key: str
    version: int
    display_name: str
    completed_sale_statuses: list[str]
    excluded_sale_statuses: list[str]
    return_treatment: str
    active_reservation_statuses: list[str]
    reservation_treatment: str
    branch_meaning: str
    warehouse_meaning: str
    quantity_interpretation: str
    revenue_interpretation: str
    currency: str
    source_timezone: str


class ConnectionProfileResponse(BaseModel):
    key: str
    display_name: str
    description: str
    adapter_type: str
    mapping: MappingProfileResponse
    capabilities: list[str]


class DataSourceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=2, max_length=120)
    connection_profile_key: str = Field(
        min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$"
    )
    mapping_profile_key: str = Field(
        min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$"
    )
    mapping_profile_version: int = Field(ge=1, le=1000)

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 2:
            raise ValueError("Display name must contain at least 2 characters.")
        return normalized


class DataSourceResponse(BaseModel):
    id: uuid.UUID
    display_name: str
    adapter_type: str
    connection_profile_key: str
    mapping: MappingProfileResponse
    status: OperationalDataSourceStatus
    last_validated_at: datetime | None
    last_successful_health_check_at: datetime | None
    failure_code: str | None
    capabilities: list[str]
    created_at: datetime
    updated_at: datetime
