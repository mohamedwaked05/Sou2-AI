"""Tenant-scoped operational data source configuration and lifecycle service."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ApplicationError
from app.database.models import (
    OperationalDataSourceConfig,
    OperationalDataSourceStatus,
    User,
)
from app.integrations.profiles import (
    ConnectionProfile,
    ConnectionProfileRegistry,
    MappingProfileError,
    OperationalMappingProfile,
)
from app.schemas.data_sources import (
    ConnectionProfileResponse,
    DataSourceCreateRequest,
    DataSourceResponse,
    MappingProfileResponse,
)
from app.schemas.operational import IntegrationHealth
from app.services.businesses import load_full_access_business

MAX_DATA_SOURCES_PER_BUSINESS = 10


@dataclass(frozen=True)
class _SourceSnapshot:
    status: OperationalDataSourceStatus
    adapter_type: str
    connection_profile_key: str
    mapping_profile_key: str
    mapping_profile_version: int
    updated_at: datetime


def _not_found() -> ApplicationError:
    return ApplicationError(
        "Data source was not found.",
        status_code=status.HTTP_404_NOT_FOUND,
        error_code="data_source_not_found",
    )


def _state_conflict(message: str) -> ApplicationError:
    return ApplicationError(
        message,
        status_code=status.HTTP_409_CONFLICT,
        error_code="data_source_state_conflict",
    )


def _mapping_response(mapping: OperationalMappingProfile) -> MappingProfileResponse:
    return MappingProfileResponse(
        key=mapping.key,
        version=mapping.version,
        display_name=mapping.display_name,
        completed_sale_statuses=list(mapping.completed_sale_statuses),
        excluded_sale_statuses=list(mapping.excluded_sale_statuses),
        return_treatment=mapping.return_treatment,
        active_reservation_statuses=list(mapping.active_reservation_statuses),
        reservation_treatment=mapping.reservation_treatment,
        branch_meaning=mapping.branch_meaning,
        warehouse_meaning=mapping.warehouse_meaning,
        quantity_interpretation=mapping.quantity_interpretation,
        revenue_interpretation=mapping.revenue_interpretation,
        currency=mapping.currency,
        source_timezone=mapping.source_timezone,
    )


def _profile_response(
    profile: ConnectionProfile, mapping: OperationalMappingProfile
) -> ConnectionProfileResponse:
    return ConnectionProfileResponse(
        key=profile.key,
        display_name=profile.display_name,
        description=profile.description,
        adapter_type=profile.adapter_type,
        mapping=_mapping_response(mapping),
        capabilities=list(mapping.required_capabilities),
    )


def _response(
    source: OperationalDataSourceConfig, registry: ConnectionProfileRegistry
) -> DataSourceResponse:
    mapping = registry.get_mapping(
        source.mapping_profile_key, source.mapping_profile_version
    )
    if mapping is None:
        raise ApplicationError(
            "The configured mapping profile is unavailable.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            error_code="operational_mapping_unavailable",
        )
    return DataSourceResponse(
        id=source.id,
        display_name=source.display_name,
        adapter_type=source.adapter_type,
        connection_profile_key=source.connection_profile_key,
        mapping=_mapping_response(mapping),
        status=source.status,
        last_validated_at=source.last_validated_at,
        last_successful_health_check_at=source.last_successful_health_check_at,
        failure_code=source.failure_code,
        capabilities=list(mapping.required_capabilities),
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


def _load_source(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> OperationalDataSourceConfig:
    load_full_access_business(session, user, business_id, for_update=for_update)
    query = select(OperationalDataSourceConfig).where(
        OperationalDataSourceConfig.id == source_id,
        OperationalDataSourceConfig.business_id == business_id,
    )
    if for_update:
        query = query.with_for_update(of=OperationalDataSourceConfig)
    source = session.scalar(query)
    if source is None:
        raise _not_found()
    return source


def _snapshot(source: OperationalDataSourceConfig) -> _SourceSnapshot:
    return _SourceSnapshot(
        status=source.status,
        adapter_type=source.adapter_type,
        connection_profile_key=source.connection_profile_key,
        mapping_profile_key=source.mapping_profile_key,
        mapping_profile_version=source.mapping_profile_version,
        updated_at=source.updated_at,
    )


def _same_snapshot(
    source: OperationalDataSourceConfig, snapshot: _SourceSnapshot
) -> bool:
    return _snapshot(source) == snapshot


def _mapping_for_source(
    source: _SourceSnapshot | OperationalDataSourceConfig,
    registry: ConnectionProfileRegistry,
) -> OperationalMappingProfile:
    profile = registry.get_profile(source.connection_profile_key)
    mapping = registry.get_mapping(
        source.mapping_profile_key, source.mapping_profile_version
    )
    if (
        profile is None
        or mapping is None
        or profile.adapter_type != source.adapter_type
        or profile.mapping_profile_key != source.mapping_profile_key
        or profile.mapping_profile_version != source.mapping_profile_version
    ):
        raise MappingProfileError("Operational source profile is unsupported.")
    mapping.validate_definition()
    return mapping


def _check_external_source(
    snapshot: _SourceSnapshot,
    registry: ConnectionProfileRegistry,
) -> tuple[IntegrationHealth, str | None]:
    checked_at = datetime.now(UTC)
    try:
        mapping = _mapping_for_source(snapshot, registry)
        adapter = registry.resolve(snapshot.connection_profile_key)
        timeout_seconds = adapter.enforced_query_timeout_seconds
        if (
            not isinstance(timeout_seconds, int)
            or isinstance(timeout_seconds, bool)
            or not 1 <= timeout_seconds <= 30
        ):
            raise MappingProfileError("Operational source timeout is unsupported.")
        health = adapter.check_health()
        mapping.validate_health(health)
        return health, None
    except MappingProfileError as exc:
        code = (
            "operational_source_unavailable"
            if str(exc) == "Operational source is unavailable."
            else "operational_mapping_invalid"
        )
    except Exception:
        code = "operational_source_unavailable"
    return (
        IntegrationHealth(
            status="unavailable",
            checked_at=checked_at,
            error_code="operational_source_unavailable",
        ),
        code,
    )


def list_connection_profiles(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    registry: ConnectionProfileRegistry,
) -> list[ConnectionProfileResponse]:
    load_full_access_business(session, user, business_id)
    responses: list[ConnectionProfileResponse] = []
    for profile in registry.available_profiles():
        mapping = registry.get_mapping(
            profile.mapping_profile_key, profile.mapping_profile_version
        )
        if mapping is None:
            continue
        try:
            mapping.validate_definition()
        except MappingProfileError:
            continue
        responses.append(_profile_response(profile, mapping))
    return responses


def create_data_source(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    body: DataSourceCreateRequest,
    registry: ConnectionProfileRegistry,
) -> DataSourceResponse:
    load_full_access_business(session, user, business_id, for_update=True)
    profile = registry.get_profile(body.connection_profile_key)
    mapping = registry.get_mapping(
        body.mapping_profile_key, body.mapping_profile_version
    )
    if profile is None:
        session.rollback()
        raise ApplicationError(
            "Choose a supported connection profile.",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            error_code="unsupported_connection_profile",
        )
    if (
        mapping is None
        or profile.mapping_profile_key != body.mapping_profile_key
        or profile.mapping_profile_version != body.mapping_profile_version
    ):
        session.rollback()
        raise ApplicationError(
            "Choose a supported mapping profile.",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            error_code="unsupported_mapping_profile",
        )
    try:
        mapping.validate_definition()
    except MappingProfileError:
        session.rollback()
        raise ApplicationError(
            "The selected mapping profile is invalid.",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            error_code="invalid_mapping_profile",
        ) from None
    count = session.scalar(
        select(func.count())
        .select_from(OperationalDataSourceConfig)
        .where(OperationalDataSourceConfig.business_id == business_id)
    )
    if count is not None and count >= MAX_DATA_SOURCES_PER_BUSINESS:
        session.rollback()
        raise ApplicationError(
            "This business has reached the data source limit.",
            status_code=status.HTTP_409_CONFLICT,
            error_code="data_source_limit_reached",
        )
    source = OperationalDataSourceConfig(
        business_id=business_id,
        display_name=body.display_name,
        adapter_type=profile.adapter_type,
        connection_profile_key=profile.key,
        mapping_profile_key=mapping.key,
        mapping_profile_version=mapping.version,
    )
    session.add(source)
    try:
        session.commit()
        session.refresh(source)
    except IntegrityError:
        session.rollback()
        raise ApplicationError(
            "The data source configuration could not be saved.",
            status_code=status.HTTP_409_CONFLICT,
            error_code="data_source_configuration_conflict",
        ) from None
    return _response(source, registry)


def list_data_sources(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    registry: ConnectionProfileRegistry,
) -> list[DataSourceResponse]:
    load_full_access_business(session, user, business_id)
    sources = session.scalars(
        select(OperationalDataSourceConfig)
        .where(OperationalDataSourceConfig.business_id == business_id)
        .order_by(
            OperationalDataSourceConfig.created_at,
            OperationalDataSourceConfig.id,
        )
        .limit(MAX_DATA_SOURCES_PER_BUSINESS)
    ).all()
    return [_response(source, registry) for source in sources]


def get_data_source(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    registry: ConnectionProfileRegistry,
) -> DataSourceResponse:
    return _response(_load_source(session, user, business_id, source_id), registry)


def validate_data_source(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    registry: ConnectionProfileRegistry,
) -> DataSourceResponse:
    source = _load_source(session, user, business_id, source_id)
    snapshot = _snapshot(source)
    session.commit()

    health, failure_code = _check_external_source(snapshot, registry)

    source = _load_source(session, user, business_id, source_id, for_update=True)
    if not _same_snapshot(source, snapshot):
        session.rollback()
        raise _state_conflict(
            "The data source changed while validation was running. Try again."
        )
    source.last_validated_at = health.checked_at
    if failure_code is None:
        source.status = (
            OperationalDataSourceStatus.ACTIVE
            if snapshot.status is OperationalDataSourceStatus.ACTIVE
            else OperationalDataSourceStatus.VALIDATED
        )
        source.last_successful_health_check_at = health.checked_at
        source.failure_code = None
    else:
        source.status = OperationalDataSourceStatus.UNHEALTHY
        source.failure_code = failure_code
    session.commit()
    session.refresh(source)
    return _response(source, registry)


def activate_data_source(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    registry: ConnectionProfileRegistry,
) -> DataSourceResponse:
    source = _load_source(session, user, business_id, source_id, for_update=True)
    if source.status is OperationalDataSourceStatus.ACTIVE:
        session.commit()
        return _response(source, registry)
    if source.status is not OperationalDataSourceStatus.VALIDATED:
        session.rollback()
        raise _state_conflict("Validate the data source before activating it.")
    other_active = session.scalar(
        select(OperationalDataSourceConfig.id).where(
            OperationalDataSourceConfig.business_id == business_id,
            OperationalDataSourceConfig.adapter_type == source.adapter_type,
            OperationalDataSourceConfig.status == OperationalDataSourceStatus.ACTIVE,
            OperationalDataSourceConfig.id != source.id,
        )
    )
    if other_active is not None:
        session.rollback()
        raise ApplicationError(
            "Another source of this type is already active.",
            status_code=status.HTTP_409_CONFLICT,
            error_code="active_data_source_conflict",
        )
    source.status = OperationalDataSourceStatus.ACTIVE
    source.failure_code = None
    try:
        session.commit()
        session.refresh(source)
    except IntegrityError:
        session.rollback()
        raise ApplicationError(
            "Another source of this type is already active.",
            status_code=status.HTTP_409_CONFLICT,
            error_code="active_data_source_conflict",
        ) from None
    return _response(source, registry)


def check_data_source_health(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    registry: ConnectionProfileRegistry,
) -> DataSourceResponse:
    source = _load_source(session, user, business_id, source_id)
    if source.status not in {
        OperationalDataSourceStatus.VALIDATED,
        OperationalDataSourceStatus.ACTIVE,
        OperationalDataSourceStatus.UNHEALTHY,
    }:
        session.rollback()
        raise _state_conflict("Validate the data source before testing its health.")
    snapshot = _snapshot(source)
    session.commit()

    health, failure_code = _check_external_source(snapshot, registry)

    source = _load_source(session, user, business_id, source_id, for_update=True)
    if not _same_snapshot(source, snapshot):
        session.rollback()
        raise _state_conflict(
            "The data source changed while the health check was running. Try again."
        )
    if failure_code is None:
        source.status = (
            OperationalDataSourceStatus.ACTIVE
            if snapshot.status is OperationalDataSourceStatus.ACTIVE
            else OperationalDataSourceStatus.VALIDATED
        )
        source.last_successful_health_check_at = health.checked_at
        source.failure_code = None
    else:
        source.status = OperationalDataSourceStatus.UNHEALTHY
        source.failure_code = failure_code
    session.commit()
    session.refresh(source)
    return _response(source, registry)


def disable_data_source(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    registry: ConnectionProfileRegistry,
) -> DataSourceResponse:
    source = _load_source(session, user, business_id, source_id, for_update=True)
    if source.status is OperationalDataSourceStatus.DISABLED:
        session.commit()
        return _response(source, registry)
    source.status = OperationalDataSourceStatus.DISABLED
    source.failure_code = None
    session.commit()
    session.refresh(source)
    return _response(source, registry)
