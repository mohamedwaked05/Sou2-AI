"""Authenticated tenant-scoped operational data source endpoints."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.database.models import User
from app.database.session import get_db_session
from app.integrations.profiles import (
    ConnectionProfileRegistry,
    get_connection_profile_registry,
)
from app.schemas.data_sources import (
    ConnectionProfileResponse,
    DataSourceCreateRequest,
    DataSourceResponse,
)
from app.services.data_sources import (
    activate_data_source,
    check_data_source_health,
    create_data_source,
    disable_data_source,
    get_data_source,
    list_connection_profiles,
    list_data_sources,
    validate_data_source,
)

router = APIRouter(
    prefix="/businesses/{business_id}/data-sources", tags=["data-sources"]
)
DatabaseSession = Annotated[Session, Depends(get_db_session)]
AuthenticatedUser = Annotated[User, Depends(get_current_user)]
ProfileRegistry = Annotated[
    ConnectionProfileRegistry, Depends(get_connection_profile_registry)
]


@router.get("/available-profiles", response_model=list[ConnectionProfileResponse])
def available_profiles(
    business_id: uuid.UUID,
    session: DatabaseSession,
    user: AuthenticatedUser,
    profiles: ProfileRegistry,
) -> list[ConnectionProfileResponse]:
    return list_connection_profiles(session, user, business_id, profiles)


@router.post("", response_model=DataSourceResponse, status_code=status.HTTP_201_CREATED)
def create(
    business_id: uuid.UUID,
    body: DataSourceCreateRequest,
    session: DatabaseSession,
    user: AuthenticatedUser,
    profiles: ProfileRegistry,
) -> DataSourceResponse:
    return create_data_source(session, user, business_id, body, profiles)


@router.get("", response_model=list[DataSourceResponse])
def list_configured(
    business_id: uuid.UUID,
    session: DatabaseSession,
    user: AuthenticatedUser,
    profiles: ProfileRegistry,
) -> list[DataSourceResponse]:
    return list_data_sources(session, user, business_id, profiles)


@router.get("/{source_id}", response_model=DataSourceResponse)
def detail(
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    session: DatabaseSession,
    user: AuthenticatedUser,
    profiles: ProfileRegistry,
) -> DataSourceResponse:
    return get_data_source(session, user, business_id, source_id, profiles)


@router.post("/{source_id}/validate", response_model=DataSourceResponse)
def validate(
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    session: DatabaseSession,
    user: AuthenticatedUser,
    profiles: ProfileRegistry,
) -> DataSourceResponse:
    return validate_data_source(session, user, business_id, source_id, profiles)


@router.post("/{source_id}/activate", response_model=DataSourceResponse)
def activate(
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    session: DatabaseSession,
    user: AuthenticatedUser,
    profiles: ProfileRegistry,
) -> DataSourceResponse:
    return activate_data_source(session, user, business_id, source_id, profiles)


@router.post("/{source_id}/health", response_model=DataSourceResponse)
def health(
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    session: DatabaseSession,
    user: AuthenticatedUser,
    profiles: ProfileRegistry,
) -> DataSourceResponse:
    return check_data_source_health(session, user, business_id, source_id, profiles)


@router.post("/{source_id}/disable", response_model=DataSourceResponse)
def disable(
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    session: DatabaseSession,
    user: AuthenticatedUser,
    profiles: ProfileRegistry,
) -> DataSourceResponse:
    return disable_data_source(session, user, business_id, source_id, profiles)
