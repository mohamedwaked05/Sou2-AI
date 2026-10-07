"""Tenant-scoped mapping proposals, review, drift gating and catalogue reads."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.agent.mapping_provider import MAPPING_MAX_OUTPUT_TOKENS, MappingProvider
from app.agent.owner_chat_provider import OwnerChatProviderError
from app.core.config import get_settings
from app.core.exceptions import ApplicationError
from app.database.models import (
    BusinessStatus,
    OperationalDataSourceConfig,
    OperationalDataSourceStatus,
    SourceMappingRevision,
    ToolCallLog,
    ToolCallStatus,
    User,
)
from app.integrations.discovery import EngineConnector, SourceMappingError
from app.integrations.mapped_products import MappedProductSource, validate_mapping
from app.integrations.profiles import ConnectionProfileRegistry, MappingProfileError
from app.schemas.data_sources import DataSourceResponse
from app.schemas.source_mapping import (
    CatalogueRequest,
    CatalogueResult,
    MappingApproveRequest,
    MappingProposal,
    MappingReview,
    ProductMapping,
    SchemaDiscovery,
)
from app.services.ai_usage import (
    _daily_limit_error,
    business_local_day_window,
    reconcile_ai_usage,
)
from app.services.businesses import load_full_access_business
from app.tools.catalogue import execute_product_search
from app.utils.argument_hashing import hash_tool_arguments


def mapping_error(code: str, status_code: int = 409) -> ApplicationError:
    return ApplicationError(
        "The catalogue mapping requires attention. Review or revalidate it before use.",
        status_code=status_code,
        error_code=code,
    )


def _source(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    *,
    lock: bool = False,
) -> OperationalDataSourceConfig:
    # Keep authorization and lifecycle behavior identical to existing onboarding.
    from app.services.data_sources import _load_source

    source = _load_source(session, user, business_id, source_id, for_update=lock)
    # Sessions retain objects after commit; snapshot the current persisted state.
    session.refresh(source)
    if source.mapping_profile_key != "discovered_products":
        raise mapping_error("mapping_discovery_not_supported", 422)
    return source


def connector_for(
    registry: ConnectionProfileRegistry, source: OperationalDataSourceConfig
) -> EngineConnector:
    profile = registry.get_profile(source.connection_profile_key)
    if (
        profile is None
        or source.business_id not in profile.business_ids
        or profile.adapter_type != source.adapter_type
        or profile.mapping_profile_key != "discovered_products"
    ):
        raise SourceMappingError("mapping_connection_not_authorized")
    try:
        return registry.connector(source.connection_profile_key, source.business_id)
    except AttributeError, MappingProfileError:
        raise SourceMappingError("mapping_connection_not_authorized") from None


def latest_approved(
    session: Session, source: OperationalDataSourceConfig
) -> SourceMappingRevision | None:
    return session.scalar(
        select(SourceMappingRevision)
        .where(
            SourceMappingRevision.source_id == source.id,
            SourceMappingRevision.business_id == source.business_id,
            SourceMappingRevision.status == "approved",
        )
        .order_by(SourceMappingRevision.version.desc())
        .limit(1)
    )


def mapped_source(
    session: Session,
    registry: ConnectionProfileRegistry,
    source: OperationalDataSourceConfig,
) -> MappedProductSource:
    revision = latest_approved(session, source)
    if revision is None:
        raise SourceMappingError("mapping_approval_required")
    try:
        discovery = SchemaDiscovery.model_validate(revision.discovery)
        connector = connector_for(registry, source)
        if (
            revision.schema_fingerprint != discovery.schema_fingerprint
            or connector.source_fingerprint != discovery.source_fingerprint
        ):
            raise SourceMappingError("mapping_schema_changed")
        return MappedProductSource(
            connector,
            discovery,
            ProductMapping.model_validate(revision.approved_mapping),
            revision.version,
        )
    except ValidationError:
        raise SourceMappingError("mapping_record_invalid") from None


def _review(revision: SourceMappingRevision) -> MappingReview:
    return MappingReview(
        id=revision.id,
        version=revision.version,
        status=revision.status,
        discovery=SchemaDiscovery.model_validate(revision.discovery),
        proposal=MappingProposal.model_validate(revision.proposal)
        if revision.proposal
        else None,
        approved_mapping=ProductMapping.model_validate(revision.approved_mapping)
        if revision.approved_mapping
        else None,
        schema_fingerprint=revision.schema_fingerprint,
        failure_code=revision.failure_code,
        validation_notes=tuple(revision.validation_notes),
    )


def discover_source(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    registry: ConnectionProfileRegistry,
) -> SchemaDiscovery:
    source = _source(session, user, business_id, source_id)
    connector = connector_for(registry, source)
    session.commit()
    try:
        return connector.discover()
    except SourceMappingError as exc:
        raise mapping_error(exc.code, 422) from None


def _existing_request(
    session: Session, source: OperationalDataSourceConfig, key: uuid.UUID
) -> SourceMappingRevision | None:
    return session.scalar(
        select(SourceMappingRevision).where(
            SourceMappingRevision.source_id == source.id,
            SourceMappingRevision.business_id == source.business_id,
            SourceMappingRevision.idempotency_key == key,
        )
    )


def _replay(session: Session, revision: SourceMappingRevision) -> MappingReview:
    if revision.status == "proposing":
        # Never redispatch a possibly billed call after a crash. Expired holds are
        # reconciled conservatively through the same restricted accounting function.
        if revision.created_at > datetime.now(UTC) - timedelta(seconds=360):
            raise mapping_error("mapping_proposal_in_progress")
        if revision.reservation_id is not None:
            reconcile_ai_usage(
                session,
                revision.reservation_id,
                usage=None,
                outcome="uncertain",
                commit=False,
            )
        revision.status = "failed"
        revision.failure_code = "mapping_proposal_interrupted"
        session.commit()
    return _review(revision)


def propose_mapping(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    key: uuid.UUID,
    registry: ConnectionProfileRegistry,
    provider: MappingProvider,
) -> MappingReview:
    source = _source(session, user, business_id, source_id, lock=True)
    connector = connector_for(registry, source)
    existing = _existing_request(session, source, key)
    if existing is not None:
        return _replay(session, existing)
    session.commit()
    try:
        discovery = connector.discover()
    except SourceMappingError as exc:
        raise mapping_error(exc.code, 422) from None
    estimated = provider.estimate_input_tokens(discovery)
    if (
        not isinstance(estimated, int)
        or isinstance(estimated, bool)
        or estimated < 0
        or estimated > 32_000
    ):
        raise mapping_error("mapping_reservation_invalid", 422)
    source = _source(session, user, business_id, source_id, lock=True)
    existing = _existing_request(session, source, key)
    if existing is not None:
        return _replay(session, existing)
    version = (
        session.scalar(
            select(func.max(SourceMappingRevision.version)).where(
                SourceMappingRevision.source_id == source.id,
            )
        )
        or 0
    ) + 1
    if version > 100:
        raise mapping_error("mapping_revision_limit")
    revision = SourceMappingRevision(
        business_id=business_id,
        source_id=source_id,
        requested_by=user.id,
        idempotency_key=key,
        version=version,
        status="proposing",
        discovery=discovery.model_dump(mode="json"),
        schema_fingerprint=discovery.schema_fingerprint,
        validation_notes=[],
    )
    session.add(revision)
    session.flush()
    business = load_full_access_business(session, user, business_id)
    try:
        row = session.execute(
            text(
                "SELECT * FROM public.sou2ai_reserve_source_mapping_usage("
                ":revision, :user, :estimated, :output, 300)"
            ),
            {
                "revision": revision.id,
                "user": user.id,
                "estimated": estimated,
                "output": MAPPING_MAX_OUTPUT_TOKENS,
            },
        ).one()
        reservation_id = row.reservation_id
        session.commit()  # Admission is committed before provider dispatch.
    except DBAPIError as exc:
        _, reset = business_local_day_window(business)
        session.rollback()
        if "daily_ai_token_limit_reached" in str(exc.orig):
            raise _daily_limit_error(reset) from None
        raise
    try:
        result = provider.propose(discovery)
        if result.proposal.mapping is not None:
            validate_mapping(result.proposal.mapping, discovery)
        revision.proposal = result.proposal.model_dump(mode="json")
        revision.status = "review"
        reconcile_ai_usage(
            session,
            reservation_id,
            usage=result.usage,
            outcome="completed"
            if result.usage is not None
            and (result.usage.authoritative or result.provider == "mock")
            else "uncertain",
            provider_identifier=result.provider,
            model_identifier=result.model,
            commit=False,
        )
    except OwnerChatProviderError as exc:
        revision.status = "failed"
        revision.failure_code = "mapping_provider_failed"
        reconcile_ai_usage(
            session,
            reservation_id,
            usage=exc.usage,
            outcome="reported_failure"
            if exc.usage is not None
            else "uncertain"
            if exc.usage_uncertain
            else "release",
            provider_identifier=exc.provider_identifier,
            model_identifier=exc.model_identifier,
            commit=False,
        )
    except SourceMappingError as exc:
        revision.status = "failed"
        revision.failure_code = exc.code
        reconcile_ai_usage(
            session,
            reservation_id,
            usage=result.usage,
            outcome="reported_failure" if result.usage else "uncertain",
            provider_identifier=result.provider,
            model_identifier=result.model,
            commit=False,
        )
    except Exception:
        revision.status = "failed"
        revision.failure_code = "mapping_provider_failed"
        reconcile_ai_usage(
            session, reservation_id, usage=None, outcome="uncertain", commit=False
        )
    session.commit()
    session.refresh(revision)
    return _review(revision)


def list_mapping_reviews(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    registry: ConnectionProfileRegistry,
) -> list[MappingReview]:
    source = _source(session, user, business_id, source_id)
    connector_for(registry, source)
    return [
        _review(item)
        for item in session.scalars(
            select(SourceMappingRevision)
            .where(
                SourceMappingRevision.business_id == business_id,
                SourceMappingRevision.source_id == source_id,
            )
            .order_by(SourceMappingRevision.version.desc())
            .limit(10)
        )
    ]


def approve_mapping(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    revision_id: uuid.UUID,
    body: MappingApproveRequest,
    registry: ConnectionProfileRegistry,
) -> MappingReview:
    source = _source(session, user, business_id, source_id)
    revision = session.scalar(
        select(SourceMappingRevision).where(
            SourceMappingRevision.id == revision_id,
            SourceMappingRevision.source_id == source_id,
            SourceMappingRevision.business_id == business_id,
        )
    )
    if revision is None:
        raise mapping_error("mapping_revision_not_found", 404)
    if revision.status == "approved":
        if revision.approved_mapping != body.mapping.model_dump(mode="json"):
            raise mapping_error("mapping_revision_immutable")
        return _review(revision)
    if (
        revision.status != "review"
        or source.status is OperationalDataSourceStatus.ACTIVE
    ):
        raise mapping_error("mapping_review_required")
    approved = latest_approved(session, source)
    if approved is not None and approved.version > revision.version:
        raise mapping_error("mapping_revision_superseded")
    expected_status, expected_updated = source.status, source.updated_at
    connector = connector_for(registry, source)
    discovery = SchemaDiscovery.model_validate(revision.discovery)
    session.commit()
    try:
        current = connector.discover()
        if (
            current.source_fingerprint != discovery.source_fingerprint
            or current.schema_fingerprint != revision.schema_fingerprint
        ):
            raise SourceMappingError("mapping_schema_changed")
        adapter = MappedProductSource(connector, discovery, body.mapping)
        notes = adapter.validate_results()
    except SourceMappingError as exc:
        raise mapping_error(exc.code, 422) from None
    source = _source(session, user, business_id, source_id, lock=True)
    session.refresh(revision)
    if (
        source.status != expected_status
        or source.updated_at != expected_updated
        or revision.status != "review"
    ):
        raise mapping_error("data_source_state_conflict")
    revision.approved_mapping = body.mapping.model_dump(mode="json")
    revision.approved_by = user.id
    revision.approved_at = datetime.now(UTC)
    revision.validation_notes = list(notes)
    revision.status = "approved"
    source.status = OperationalDataSourceStatus.VALIDATED
    source.last_validated_at = revision.approved_at
    source.last_successful_health_check_at = revision.approved_at
    source.failure_code = None
    session.commit()
    return _review(revision)


def validate_mapped_source(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    registry: ConnectionProfileRegistry,
) -> DataSourceResponse:
    from app.services.data_sources import _response

    source = _source(session, user, business_id, source_id)
    try:
        adapter = mapped_source(session, registry, source)
    except SourceMappingError as exc:
        raise mapping_error(exc.code) from None
    expected_status, expected_updated = source.status, source.updated_at
    session.commit()
    failure = None
    try:
        current = adapter.connector.discover()
        if (
            current.source_fingerprint != adapter.discovery.source_fingerprint
            or current.schema_fingerprint != adapter.discovery.schema_fingerprint
        ):
            raise SourceMappingError("mapping_schema_changed")
        adapter.validate_results()
    except SourceMappingError as exc:
        failure = exc.code
    source = _source(session, user, business_id, source_id, lock=True)
    if source.status != expected_status or source.updated_at != expected_updated:
        raise mapping_error("data_source_state_conflict")
    source.last_validated_at = datetime.now(UTC)
    source.failure_code = failure
    if failure:
        source.status = OperationalDataSourceStatus.UNHEALTHY
    else:
        source.status = (
            OperationalDataSourceStatus.ACTIVE
            if expected_status is OperationalDataSourceStatus.ACTIVE
            else OperationalDataSourceStatus.VALIDATED
        )
        source.last_successful_health_check_at = source.last_validated_at
    session.commit()
    return _response(source, registry, session)


def search_catalogue(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    body: CatalogueRequest,
    registry: ConnectionProfileRegistry,
) -> CatalogueResult:
    source = _source(session, user, business_id, source_id)
    business = load_full_access_business(session, user, business_id)
    if business.status is not BusinessStatus.ACTIVE:
        raise mapping_error("inactive_business", 403)
    if source.status is not OperationalDataSourceStatus.ACTIVE:
        raise mapping_error("mapping_source_not_active")
    secret = get_settings().tool_call_audit_hmac_secret
    if secret is None or not secret.get_secret_value().strip():
        raise mapping_error("audit_unavailable", 503)
    args_hash = hash_tool_arguments(
        {"source_id": str(source_id), **body.model_dump()}, secret
    )
    try:
        adapter = mapped_source(session, registry, source)
    except SourceMappingError as exc:
        raise mapping_error(exc.code) from None
    expected_updated = source.updated_at
    if (
        body.mapping_version is not None
        and body.mapping_version != adapter.revision_version
    ):
        raise mapping_error("mapping_selection_stale")
    session.commit()
    started = time.monotonic()
    try:
        result = execute_product_search(adapter, body)
    except SourceMappingError as exc:
        source = _source(session, user, business_id, source_id, lock=True)
        if exc.code not in {
            "empty_product_search",
            "invalid_product_identifier",
            "mapping_selection_stale",
        }:
            source.status = OperationalDataSourceStatus.UNHEALTHY
            source.last_validated_at = datetime.now(UTC)
            source.failure_code = exc.code
        session.add(
            ToolCallLog(
                business_id=business_id,
                user_id=user.id,
                tool_name="product_search",
                args_hash=args_hash,
                status=ToolCallStatus.ERROR,
                error_code="adapter_failure",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        )
        session.commit()
        raise mapping_error(exc.code) from None
    # Source may have been disabled while the external bounded SELECT ran.
    session.refresh(source)
    if (
        source.status is not OperationalDataSourceStatus.ACTIVE
        or source.updated_at != expected_updated
    ):
        raise mapping_error("mapping_source_not_active")
    session.add(
        ToolCallLog(
            business_id=business_id,
            user_id=user.id,
            tool_name="product_search",
            args_hash=args_hash,
            status=ToolCallStatus.SUCCESS,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    )
    session.commit()
    return result
