"""Reuse source authorization, discovery and audit for reviewed operational reads."""

import time
import uuid
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.mapping_provider import operational_metadata_proposals
from app.core.admission import require_open_admission
from app.core.config import get_settings
from app.database.models import (
    BusinessStatus,
    OperationalDataSourceConfig,
    OperationalDataSourceStatus,
    SourceOperationalMappingRevision,
    ToolCallLog,
    ToolCallStatus,
    User,
)
from app.integrations.discovery import SourceMappingError
from app.integrations.mapped_operational import (
    MappedOperationalSource,
    validate_operational_mapping,
)
from app.integrations.profiles import ConnectionProfileRegistry
from app.schemas.operational_mapping import (
    OperationalMapping,
    OperationalMappingApproval,
    OperationalMappingDraft,
    OperationalMappingPreview,
    OperationalRead,
    OperationalReport,
)
from app.schemas.source_mapping import SchemaDiscovery
from app.services.businesses import load_full_access_business
from app.services.source_mapping import (
    _source,
    connector_for,
    latest_approved,
    mapped_source,
    mapping_error,
)
from app.utils.argument_hashing import hash_tool_arguments


def operational_onboarding(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    registry: ConnectionProfileRegistry,
) -> dict:
    """Metadata-only proposal workspace. Names never establish business semantics."""
    require_open_admission()
    source = _source(session, user, business_id, source_id)
    try:
        catalogue = mapped_source(session, registry, source)
        if catalogue.quality_policy is not None:
            raise SourceMappingError("catalogue_partial_operations_unapproved")
        discovery = catalogue.connector.discover()
        if discovery != catalogue.discovery:
            raise SourceMappingError("mapping_schema_changed")
    except SourceMappingError as exc:
        raise mapping_error(exc.code) from None
    history = session.scalars(
        select(SourceOperationalMappingRevision)
        .where(
            SourceOperationalMappingRevision.business_id == business_id,
            SourceOperationalMappingRevision.source_id == source_id,
        )
        .order_by(SourceOperationalMappingRevision.version.desc())
        .limit(50)
    ).all()
    return {
        "discovery": discovery.model_dump(mode="json"),
        "context": {
            "business_id": str(business_id),
            "source_id": str(source_id),
            "catalogue_revision": catalogue.revision_version,
            "source_fingerprint": discovery.source_fingerprint,
            "schema_fingerprint": discovery.schema_fingerprint,
        },
        "product_mapping": catalogue.mapping.model_dump(mode="json"),
        "mapping_schema": OperationalMapping.model_json_schema(),
        "proposal_mode": "owner_authored_metadata_only",
        "proposals": operational_metadata_proposals(discovery, catalogue.mapping),
        "limitations": [
            "Metadata cannot prove quantities, units, status or accounting rules.",
            "Supply authoritative semantics; unsupported capabilities remain disabled.",
            "Preview is bounded evidence, not semantic certification or approval.",
            "Each approval replaces the capability set; retain desired mappings.",
        ],
        "history": [
            {
                "id": str(r.id),
                "version": r.version,
                "mapping": r.mapping,
                "discovery": r.discovery,
            }
            for r in history
        ],
    }


def _draft_source(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    body: OperationalMappingDraft,
    registry: ConnectionProfileRegistry,
) -> tuple[OperationalDataSourceConfig, MappedOperationalSource]:
    require_open_admission()
    source = _source(session, user, business_id, source_id)
    if source.status is OperationalDataSourceStatus.ACTIVE:
        raise mapping_error("mapping_disable_before_approval")
    try:
        catalogue = mapped_source(session, registry, source)
        if catalogue.quality_policy is not None:
            raise SourceMappingError("catalogue_partial_operations_unapproved")
        discovery = catalogue.connector.discover()
        if discovery != body.discovery or discovery != catalogue.discovery:
            raise SourceMappingError("mapping_schema_changed")
        adapter = MappedOperationalSource(
            catalogue, body.mapping, 1, business_id=business_id, source_id=source_id
        )
    except SourceMappingError as exc:
        raise mapping_error(exc.code) from None
    return source, adapter


def validate_operational_draft(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    body: OperationalMappingDraft,
    registry: ConnectionProfileRegistry,
) -> dict:
    _, adapter = _draft_source(session, user, business_id, source_id, body, registry)
    return {
        "status": "structurally_valid",
        "approved": False,
        "capabilities": adapter.mapping.capabilities,
        "limitations": [
            "Owner review of source semantics and row isolation is required."
        ],
    }


def preview_operational_draft(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    body: OperationalMappingPreview,
    registry: ConnectionProfileRegistry,
) -> dict:
    source, adapter = _draft_source(
        session, user, business_id, source_id, body, registry
    )
    secret = get_settings().tool_call_audit_hmac_secret
    if secret is None or not secret.get_secret_value().strip():
        raise mapping_error("audit_unavailable", 503)
    # Preview revisions describe an ephemeral draft, never persisted authority.
    request = body.request.model_copy(update={"mapping_revision": 1})
    args_hash = hash_tool_arguments(body.model_dump(mode="json"), secret)
    expected = source.updated_at
    session.commit()
    started = time.monotonic()
    code, report = None, None
    try:
        report = adapter.read(request)
        if time.monotonic() - started > adapter.enforced_query_timeout_seconds:
            raise SourceMappingError("source_query_timeout")
        session.refresh(source)
        current = latest_approved(session, source)
        if (
            source.updated_at != expected
            or source.status is OperationalDataSourceStatus.ACTIVE
            or current is None
            or current.version != adapter.mapping.catalogue_revision
        ):
            raise SourceMappingError("mapping_selection_stale")
    except SourceMappingError as exc:
        code = exc.code
    session.add(
        ToolCallLog(
            business_id=business_id,
            user_id=user.id,
            tool_name="operational_mapping_preview",
            args_hash=args_hash,
            status=ToolCallStatus.ERROR if code else ToolCallStatus.SUCCESS,
            error_code="adapter_failure" if code else None,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    )
    session.commit()
    if code:
        raise mapping_error(code)
    return {"approved": False, "draft": True, "report": report.model_dump(mode="json")}


def latest_operational_approval(
    session: Session, source: OperationalDataSourceConfig
) -> SourceOperationalMappingRevision | None:
    return session.scalar(
        select(SourceOperationalMappingRevision)
        .where(
            SourceOperationalMappingRevision.source_id == source.id,
            SourceOperationalMappingRevision.business_id == source.business_id,
        )
        .order_by(SourceOperationalMappingRevision.version.desc())
        .limit(1)
    )


def operational_source(
    session: Session,
    registry: ConnectionProfileRegistry,
    source: OperationalDataSourceConfig,
    operation: str | None = None,
    rank_by: str | None = None,
) -> MappedOperationalSource:
    if operation is not None:
        from app.services.capabilities import (
            approved_adapter,
            capability_family,
            latest_event,
        )

        family = capability_family(operation, rank_by)
        if family and latest_event(session, source, family) is not None:
            return approved_adapter(
                session, registry, source, operation, rank_by=rank_by
            )
    revision = latest_operational_approval(session, source)
    if revision is None:
        raise SourceMappingError("operational_mapping_approval_required")
    catalogue = mapped_source(session, registry, source)
    if catalogue.quality_policy is not None:
        raise SourceMappingError("catalogue_partial_operations_unapproved")
    try:
        discovery = SchemaDiscovery.model_validate(revision.discovery)
        mapping = OperationalMapping.model_validate(revision.mapping)
        if discovery != catalogue.discovery:
            raise SourceMappingError("mapping_schema_changed")
        from app.integrations.mapped_products import MappedProductSource

        catalogue = MappedProductSource(
            catalogue.connector,
            discovery,
            catalogue.mapping,
            catalogue.revision_version,
        )
        return MappedOperationalSource(
            catalogue,
            mapping,
            revision.version,
            business_id=source.business_id,
            source_id=source.id,
        )
    except ValidationError:
        raise SourceMappingError("operational_mapping_record_invalid") from None


def approve_operational_mapping(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    body: OperationalMappingApproval,
    registry: ConnectionProfileRegistry,
) -> dict:
    require_open_admission()
    source = _source(session, user, business_id, source_id, lock=True)
    if source.status is OperationalDataSourceStatus.ACTIVE:
        raise mapping_error("mapping_disable_before_approval")
    if body.mapping.business_id != business_id or body.mapping.source_id != source_id:
        raise mapping_error("mapping_scope_mismatch", 403)
    catalogue = latest_approved(session, source)
    if catalogue is None or catalogue.version != body.mapping.catalogue_revision:
        raise mapping_error("mapping_selection_stale")
    from app.services.catalogue_quality import approved_coverage

    try:
        policy, _ = approved_coverage(session, source, catalogue)
        if policy is not None:
            raise SourceMappingError("catalogue_partial_operations_unapproved")
    except SourceMappingError as exc:
        raise mapping_error(exc.code) from None
    connector = connector_for(registry, source)
    from app.schemas.source_mapping import ProductMapping

    try:
        discovery = connector.discover()
        if discovery != body.discovery or discovery != SchemaDiscovery.model_validate(
            catalogue.discovery
        ):
            raise SourceMappingError("mapping_schema_changed")
        products = ProductMapping.model_validate(catalogue.approved_mapping)
        validate_operational_mapping(body.mapping, discovery, products)
    except SourceMappingError as exc:
        raise mapping_error(exc.code) from None
    except ValidationError:
        raise mapping_error("mapping_record_invalid") from None
    previous = latest_operational_approval(session, source)
    revision = SourceOperationalMappingRevision(
        id=uuid.uuid4(),
        business_id=business_id,
        source_id=source_id,
        catalogue_revision_id=catalogue.id,
        version=previous.version + 1 if previous else 1,
        mapping=body.mapping.model_dump(mode="json"),
        discovery=discovery.model_dump(mode="json"),
        approved_by=user.id,
    )
    session.add(revision)
    session.commit()
    return {
        "id": str(revision.id),
        "version": revision.version,
        "capabilities": body.mapping.capabilities,
    }


def read_operational_report(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    request: OperationalRead,
    registry: ConnectionProfileRegistry,
) -> OperationalReport:
    require_open_admission()
    source = _source(session, user, business_id, source_id)
    if (
        load_full_access_business(session, user, business_id).status
        is not BusinessStatus.ACTIVE
    ):
        raise mapping_error("inactive_business", 403)
    from app.services.capabilities import capability_family, latest_event

    family = capability_family(request.capability, request.rank_by)
    independent = latest_event(session, source, family) is not None
    if (
        not independent and source.status is not OperationalDataSourceStatus.ACTIVE
    ) or source.status is OperationalDataSourceStatus.DISABLED:
        raise mapping_error("mapping_source_not_active")
    secret = get_settings().tool_call_audit_hmac_secret
    if secret is None or not secret.get_secret_value().strip():
        raise mapping_error("audit_unavailable", 503)
    args_hash = hash_tool_arguments(request.model_dump(mode="json"), secret)
    try:
        adapter = operational_source(
            session, registry, source, request.capability, request.rank_by
        )
    except SourceMappingError as exc:
        raise mapping_error(exc.code) from None
    expected = source.updated_at
    session.commit()
    started = time.monotonic()
    code, result = None, None
    try:
        result = adapter.read(request)
        if time.monotonic() - started > adapter.enforced_query_timeout_seconds:
            raise SourceMappingError("source_query_timeout")
        session.refresh(source)
        current = latest_operational_approval(session, source)
        product = latest_approved(session, source)
        if independent:
            event = latest_event(session, source, family)
            if (
                source.status is OperationalDataSourceStatus.DISABLED
                or event is None
                or event.id != adapter.capability_approval_id
                or event.approval_type == "revoked"
                or event.expires_at <= datetime.now(UTC)
            ):
                raise SourceMappingError("mapping_selection_stale")
        elif (
            source.status is not OperationalDataSourceStatus.ACTIVE
            or source.updated_at != expected
            or current is None
            or current.version != adapter.revision
            or product is None
            or product.version != adapter.mapping.catalogue_revision
        ):
            raise SourceMappingError("mapping_selection_stale")
    except SourceMappingError as exc:
        code = exc.code
    session.add(
        ToolCallLog(
            business_id=business_id,
            user_id=user.id,
            tool_name="mapped_operational_report",
            args_hash=args_hash,
            status=ToolCallStatus.ERROR if code else ToolCallStatus.SUCCESS,
            error_code="adapter_failure" if code else None,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    )
    session.commit()
    if code:
        raise mapping_error(code)
    return result
