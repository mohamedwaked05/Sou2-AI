"""Independent capability workflows backed by the revision-19 quality ledger."""

import hmac
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.admission import require_open_admission
from app.core.config import get_settings
from app.database.models import (
    CatalogueQualityApproval,
    CatalogueQualityReportRecord,
    OperationalDataSourceConfig,
    OperationalDataSourceStatus,
    User,
)
from app.integrations.discovery import EngineConnector, SourceMappingError
from app.integrations.mapped_operational import (
    MappedOperationalSource,
    OperationalSourceContext,
)
from app.integrations.operational_quality import (
    FAMILY_TOOLS,
    evaluate_operational,
    execution_columns,
    execution_discovery,
    fact_definitions,
    family_mapping,
)
from app.integrations.profiles import ConnectionProfileRegistry
from app.schemas.capabilities import (
    CapabilityActivation,
    CapabilityApproval,
    CapabilityQualityReport,
    CapabilityState,
    CapabilityValidation,
    CapabilityWorkspace,
)
from app.schemas.operational_mapping import OperationalMapping
from app.schemas.source_mapping import ProductMapping, SchemaDiscovery
from app.services.catalogue_quality import (
    _new_event,
    _serialize_validation,
    report_integrity,
)
from app.services.source_mapping import (
    _source,
    connector_for,
    latest_approved,
    mapping_error,
)

FAMILIES = tuple(FAMILY_TOOLS)
SEMANTICS = {
    "inventory": (
        "Identify an authoritative balance, complete balance grain,"
        " product identity, unit and location; confirm reservation "
        "and restocking semantics."
    ),
    "pricing": (
        "Identify selling amount, currency, unit, price list, "
        "location, tax basis and effective-date precedence."
    ),
    "sales": (
        "Identify fact/transaction keys, completed and excluded "
        "statuses, quantity sign, event timestamp/timezone, scope "
        "and returns policy."
    ),
    "financial": (
        "Identify complete sales/refund ledgers, gross and net "
        "source amounts, discounts, currency, tax basis and refund "
        "recognition. Never omit invalid financial events."
    ),
}


def reject_legacy_approval(
    session: Session, user: User, business_id: uuid.UUID, source_id: uuid.UUID
) -> None:
    """The old HTTP checkbox cannot authorize a new capability quality policy."""
    require_open_admission()
    _source(session, user, business_id, source_id)
    from app.core.exceptions import ApplicationError

    raise ApplicationError(
        "Validate and review each capability's quality report, then approve "
        "its exact business meaning and record counts in Data Sources.",
        status_code=409,
        error_code="capability_quality_review_required",
    )


def capability_family(operation: str, rank_by: str | None = None) -> str | None:
    if operation in {"revenue", "refunds"} or (
        operation == "best_sellers" and rank_by == "net_revenue"
    ):
        return "financial"
    return next(
        (family for family, tools in FAMILY_TOOLS.items() if operation in tools), None
    )


def latest_event(
    session: Session, source: OperationalDataSourceConfig, family: str
) -> CatalogueQualityApproval | None:
    return session.scalar(
        select(CatalogueQualityApproval)
        .where(
            CatalogueQualityApproval.source_id == source.id,
            CatalogueQualityApproval.business_id == source.business_id,
            CatalogueQualityApproval.capability == family,
        )
        .order_by(CatalogueQualityApproval.version.desc())
        .limit(1)
    )


def _record(
    session: Session,
    source: OperationalDataSourceConfig,
    family: str,
    report_id: uuid.UUID | None = None,
) -> CatalogueQualityReportRecord | None:
    query = select(CatalogueQualityReportRecord).where(
        CatalogueQualityReportRecord.source_id == source.id,
        CatalogueQualityReportRecord.business_id == source.business_id,
        CatalogueQualityReportRecord.capability == family,
    )
    if report_id:
        query = query.where(CatalogueQualityReportRecord.id == report_id)
    return session.scalar(
        query.order_by(
            CatalogueQualityReportRecord.created_at.desc(),
            CatalogueQualityReportRecord.id.desc(),
        ).limit(1)
    )


def checked_report(
    record: CatalogueQualityReportRecord | None,
) -> CapabilityQualityReport:
    if record is None:
        raise SourceMappingError("capability_report_required")
    try:
        report = CapabilityQualityReport.model_validate(record.report)
        if (
            report.integrity != report_integrity(report)
            or report.mapping.model_dump(mode="json") != record.mapping
        ):
            raise ValueError
        return report
    except ValidationError, ValueError:
        raise SourceMappingError("capability_report_integrity_invalid") from None


def _adapter(
    connector: EngineConnector,
    mapping: OperationalMapping,
    discovery: SchemaDiscovery,
    identity: ProductMapping | None,
    version: int,
) -> MappedOperationalSource:
    catalogue = OperationalSourceContext(
        connector, discovery, mapping.catalogue_revision, identity
    )
    adapter = MappedOperationalSource(
        catalogue,
        mapping,
        version,
        business_id=mapping.business_id,
        source_id=mapping.source_id,
    )
    adapter.independent = True
    return adapter


def _identity_signature(mapping: ProductMapping) -> tuple:
    return (mapping.object_id, mapping.key_columns, mapping.row_scopes)


def approved_adapter(
    session: Session,
    registry: ConnectionProfileRegistry,
    source: OperationalDataSourceConfig,
    operation: str,
    *,
    rank_by: str | None = None,
) -> MappedOperationalSource:
    family = capability_family(operation, rank_by)
    event = latest_event(session, source, family)
    if event is None:
        raise SourceMappingError("capability_approval_required")
    if source.status is OperationalDataSourceStatus.DISABLED:
        raise SourceMappingError("mapping_source_disabled")
    if event.approval_type == "revoked":
        raise SourceMappingError("capability_approval_revoked")
    if event.expires_at <= datetime.now(UTC):
        raise SourceMappingError("capability_approval_expired")
    report = checked_report(_record(session, source, family, event.report_id))
    if not report.eligible or report.invalid_records != 0:
        raise SourceMappingError("capability_evidence_invalid")
    if report.max_invalid_percent != get_settings().catalogue_max_invalid_percent:
        raise SourceMappingError("capability_policy_changed")
    current = latest_approved(session, source)
    if any(
        f.product_reference == "catalogue_identity"
        for f in fact_definitions(report.mapping)
    ):
        if current is None or _identity_signature(
            ProductMapping.model_validate(current.approved_mapping)
        ) != _identity_signature(report.identity_mapping):
            raise SourceMappingError("capability_identity_mapping_changed")
    connector = connector_for(registry, source)
    adapter = _adapter(
        connector,
        report.mapping,
        report.discovery,
        report.identity_mapping,
        event.version,
    )
    allowed = set(FAMILY_TOOLS[family]) & set(report.mapping.capabilities)
    if operation not in allowed and not (
        family == "financial" and operation == "best_sellers"
    ):
        raise SourceMappingError("capability_operation_unapproved")
    adapter.capability_approval_id = event.id
    adapter.business_meaning = report.meaning
    adapter.capability_family = family
    adapter.resolution_catalogue = None
    if source.status is OperationalDataSourceStatus.ACTIVE:
        from app.services.source_mapping import mapped_source

        try:
            adapter.resolution_catalogue = mapped_source(session, registry, source)
        except SourceMappingError:
            pass
    return adapter


def validate_capability(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    family: str,
    body: CapabilityValidation,
    registry: ConnectionProfileRegistry,
) -> CapabilityQualityReport:
    require_open_admission()
    source = _source(session, user, business_id, source_id)
    _serialize_validation(session, source)
    revision = latest_approved(session, source)
    if body.mapping.business_id != business_id or body.mapping.source_id != source_id:
        raise mapping_error("mapping_scope_mismatch", 403)
    connector = connector_for(registry, source)
    expected_updated = source.updated_at
    selected = family_mapping(body.mapping, family)
    needs_catalogue = any(
        f.product_reference == "catalogue_identity" for f in fact_definitions(selected)
    )
    if needs_catalogue and revision is None:
        raise mapping_error("capability_identity_mapping_required")
    if needs_catalogue and body.mapping.catalogue_revision != revision.version:
        raise mapping_error("mapping_selection_stale")
    identity = (
        ProductMapping.model_validate(revision.approved_mapping)
        if needs_catalogue
        else None
    )
    if not needs_catalogue:
        if len(registry.get_profile(source.connection_profile_key).business_ids) > 1:
            raise mapping_error("mapping_business_partition_evidence_required")
        selected = selected.model_copy(update={"catalogue_revision": 0})
    payload = None
    try:
        # Do not substitute a different schema supplied by a caller.
        if body.discovery.source_fingerprint != connector.source_fingerprint:
            raise SourceMappingError("mapping_source_changed")
        expected = execution_discovery(selected, body.discovery, identity)
        from app.integrations.mapped_operational import validate_operational_mapping

        validate_operational_mapping(
            selected.model_copy(
                update={"schema_fingerprint": expected.schema_fingerprint}
            ),
            expected,
            identity,
        )
        columns, _ = execution_columns(selected, identity)
        discovery = connector.discover(object_ids=set(columns), column_scope=columns)
        # A caller cannot fabricate types, keys or composite relationships.
        expected_objects = tuple(
            obj.model_copy(
                update={
                    "columns": tuple(
                        c
                        for c in obj.columns
                        if c.name in columns.get(obj.object_id, set())
                    ),
                    "unique_keys": tuple(
                        k
                        for k in obj.unique_keys
                        if set(k) <= columns.get(obj.object_id, set())
                    ),
                    "validation_stamp": None,
                }
            )
            for obj in expected.objects
            if obj.object_id in columns
        )
        expected_relations = tuple(
            r
            for r in expected.relationships
            if set(r.source_columns) <= columns.get(r.source_object_id, set())
            and set(r.target_columns) <= columns.get(r.target_object_id, set())
        )
        expected = SchemaDiscovery(
            engine=expected.engine,
            source_fingerprint=expected.source_fingerprint,
            objects=expected_objects,
            relationships=expected_relations,
        )
        comparable = discovery.model_copy(
            update={
                "objects": tuple(
                    o.model_copy(update={"validation_stamp": None})
                    for o in discovery.objects
                )
            }
        )
        if expected != comparable:
            raise SourceMappingError("mapping_schema_changed")
        selected = selected.model_copy(
            update={"schema_fingerprint": discovery.schema_fingerprint}
        )
        now = datetime.now(UTC)
        payload = dict(
            id=uuid.uuid4(),
            business_id=business_id,
            source_id=source_id,
            revision_id=revision.id if needs_catalogue else None,
            capability=family,
            mapping=selected,
            discovery=discovery,
            identity_mapping=identity,
            meaning=body.meaning,
            source_fingerprint=discovery.source_fingerprint,
            schema_fingerprint=discovery.schema_fingerprint,
            validated_at=now,
            expires_at=now
            + timedelta(seconds=get_settings().catalogue_quality_report_ttl_seconds),
            max_invalid_percent=get_settings().catalogue_max_invalid_percent,
            structural_status="INCOMPLETE",
            impact="INSUFFICIENT_CAPABILITY_EVIDENCE",
            findings=(),
        )
        adapter = _adapter(connector, selected, discovery, identity, 1)
        with connector.connection(consistent=True) as connection:
            result = evaluate_operational(adapter, connection)
        payload.update(
            total_records=result.total,
            invalid_records=result.invalid,
            valid_records=result.total - result.invalid,
            invalid_percent=Decimal(result.invalid) * 100 / result.total
            if result.total
            else Decimal(0),
            reason_counts=result.reasons,
            record_counts=result.records,
            data_digest=result.digest,
            structural_status="BLOCKED"
            if result.reasons.get("duplicate_fact")
            else "PASS",
            impact="BLOCKED_BY_MAPPING"
            if result.reasons.get("duplicate_fact")
            else "BLOCKED_BY_DATA_QUALITY"
            if result.invalid
            else "SAFE_FOR_CAPABILITY",
            eligible=not result.invalid,
            diagnostics=result.diagnostics,
            diagnostics_truncated=result.invalid > len(result.diagnostics),
            findings=(
                (
                    "This capability retains every relevant in-scope fact. "
                    "Operational invalid rows are never silently excluded."
                ),
                (
                    "Catalogue names and unrelated incoming database "
                    "relationships do not participate in this contract."
                ),
                (
                    "Financial and quantity completeness require zero invalid "
                    "facts; the configured tolerance cannot bypass that "
                    "requirement."
                ),
            ),
        )
        report = CapabilityQualityReport(**payload)
    except SourceMappingError as exc:
        if payload is None:
            raise mapping_error(exc.code, 422) from None
        incomplete = exc.code.startswith(
            ("source_", "discovery_", "catalogue_validation_")
        )
        payload.update(
            structural_status="INCOMPLETE" if incomplete else "BLOCKED",
            impact="INSUFFICIENT_CAPABILITY_EVIDENCE"
            if incomplete
            else "BLOCKED_BY_MAPPING",
            eligible=False,
            findings=(
                exc.code,
                (
                    "Resolve this capability's mapping or reader-access "
                    "prerequisite, then revalidate."
                ),
            ),
        )
        report = CapabilityQualityReport(**payload)
    source = _source(session, user, business_id, source_id, lock=True)
    if source.updated_at != expected_updated:
        raise mapping_error("data_source_state_conflict")
    while len(report.model_dump_json().encode()) > 90_000 and report.diagnostics:
        report = report.model_copy(
            update={
                "diagnostics": report.diagnostics[:-1],
                "diagnostics_truncated": True,
            }
        )
    report = report.model_copy(update={"integrity": report_integrity(report)})
    if len(report.model_dump_json().encode()) > 120_000:
        raise mapping_error("capability_report_budget_exceeded")
    session.add(
        CatalogueQualityReportRecord(
            id=report.id,
            business_id=business_id,
            source_id=source_id,
            capability=family,
            revision_id=revision.id if needs_catalogue else None,
            requested_by=user.id,
            mapping=selected.model_dump(mode="json"),
            source_updated_at=expected_updated,
            report=report.model_dump(mode="json"),
        )
    )
    session.commit()
    return report


def get_capability_report(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    family: str,
    report_id: uuid.UUID | None = None,
) -> CapabilityQualityReport | None:
    source = _source(session, user, business_id, source_id)
    record = _record(session, source, family, report_id)
    if record is None:
        return None
    try:
        return checked_report(record)
    except SourceMappingError as exc:
        raise mapping_error(exc.code) from None


def approve_capability(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    family: str,
    body: CapabilityApproval,
    registry: ConnectionProfileRegistry,
) -> CapabilityActivation:
    require_open_admission()
    source = _source(session, user, business_id, source_id, lock=True)
    _serialize_validation(session, source)
    record = _record(session, source, family, body.report_id)
    try:
        report = checked_report(record)
        if (
            not hmac.compare_digest(report.integrity, body.integrity)
            or not report.eligible
            or report.invalid_records != 0
            or body.total_records != report.total_records
        ):
            raise SourceMappingError("capability_confirmation_invalid")
        event = latest_event(session, source, family)
        previous = session.scalar(
            select(CatalogueQualityApproval).where(
                CatalogueQualityApproval.report_id == report.id,
                CatalogueQualityApproval.approval_type == "full",
            )
        )
        if previous is not None:
            if event is None or event.id != previous.id:
                raise SourceMappingError("capability_approval_revoked")
            adapter = approved_adapter(
                session, registry, source, FAMILY_TOOLS[family][0]
            )
            with adapter.catalogue.connector.connection(consistent=True) as connection:
                if evaluate_operational(
                    adapter, connection, diagnostics=False, fingerprint=False
                ).invalid:
                    raise SourceMappingError("capability_data_quality_changed")
            return CapabilityActivation(
                approval_id=event.id, version=event.version, capability=family
            )
        if (
            report.expires_at <= datetime.now(UTC)
            or _record(session, source, family).id != report.id
            or report.max_invalid_percent
            != get_settings().catalogue_max_invalid_percent
        ):
            raise SourceMappingError("capability_report_stale")
        if source.status is OperationalDataSourceStatus.DISABLED:
            raise SourceMappingError("mapping_source_disabled")
        if report.identity_mapping is not None:
            current = latest_approved(session, source)
            if current is None or _identity_signature(
                ProductMapping.model_validate(current.approved_mapping)
            ) != _identity_signature(report.identity_mapping):
                raise SourceMappingError("capability_identity_mapping_changed")
        connector = connector_for(registry, source)
        adapter = _adapter(
            connector, report.mapping, report.discovery, report.identity_mapping, 1
        )
        with connector.connection(consistent=True) as connection:
            result = evaluate_operational(adapter, connection, diagnostics=False)
            if (
                result.invalid
                or result.digest != report.data_digest
                or result.total != report.total_records
            ):
                raise SourceMappingError("capability_report_stale")
            event = _new_event(
                session,
                source,
                user,
                report.id,
                "full",
                datetime.now(UTC)
                + timedelta(
                    seconds=get_settings().catalogue_partial_approval_ttl_seconds
                ),
                family,
            )
            session.flush()
            # The consistent source snapshot remains held through the platform commit.
            session.commit()
        return CapabilityActivation(
            approval_id=event.id, version=event.version, capability=family
        )
    except SourceMappingError as exc:
        raise mapping_error(exc.code) from None


def revoke_capability(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    family: str,
) -> None:
    require_open_admission()
    source = _source(session, user, business_id, source_id, lock=True)
    _serialize_validation(session, source)
    previous = latest_event(session, source, family)
    if previous is not None and previous.approval_type != "revoked":
        _new_event(
            session,
            source,
            user,
            previous.report_id,
            "revoked",
            previous.expires_at,
            family,
        )
    session.commit()


def active_capability_tools(
    session: Session,
    registry: ConnectionProfileRegistry,
    source: OperationalDataSourceConfig,
) -> tuple[str, ...]:
    tools = []
    for names in FAMILY_TOOLS.values():
        try:
            adapter = approved_adapter(session, registry, source, names[0])
            tools.extend(n for n in names if n in adapter.mapping.capabilities)
        except SourceMappingError:
            continue
    return tuple(tools)


def capability_workspace(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    registry: ConnectionProfileRegistry,
) -> CapabilityWorkspace:
    require_open_admission()
    source = _source(session, user, business_id, source_id)
    connector = connector_for(registry, source)
    connection_status = "CONNECTED"
    discovery, failure = None, None
    try:
        discovery = connector.discover()
    except SourceMappingError as exc:
        try:
            discovery = connector.discover(available_only=True)
            connection_status = "CONNECTED_READ_SCOPE_LIMITED"
        except SourceMappingError:
            connection_status, failure = "SOURCE_UNAVAILABLE", exc.code
    revision = latest_approved(session, source)
    identity = (
        ProductMapping.model_validate(revision.approved_mapping) if revision else None
    )
    from app.agent.mapping_provider import operational_metadata_proposals

    proposals = operational_metadata_proposals(discovery, identity) if discovery else {}
    candidates = tuple(
        c for c in proposals.get("fact_candidates", ()) if c["numeric_fields"]
    )
    try:
        access_inventory = connector.accessible_metadata()
    except SourceMappingError as exc:
        access_inventory = {"status": "UNAVAILABLE", "reason": exc.code}
    states = []
    for family in FAMILIES:
        record = _record(session, source, family)
        report = checked_report(record) if record else None
        latest_validation = report
        event = latest_event(session, source, family)
        state = "MAPPING_IN_REVIEW" if report else "MAPPING_REQUIRED"
        runtime_failure, runtime_invalid = None, None
        prerequisites = [SEMANTICS[family]]
        if not candidates:
            prerequisites.insert(
                0,
                (
                    "No approved readable fact candidate with an enforced key "
                    "is available. Confirm the source object's exact SELECT "
                    "column scope and authoritative semantics; do not infer "
                    "that the database lacks this data."
                ),
            )
        if identity is None:
            prerequisites.insert(
                0,
                (
                    "Catalogue-linked lookups need an approved identity mapping. "
                    "Independent fact identities and transaction totals may "
                    "be reviewed without it."
                ),
            )
        if report:
            state = "VALIDATED" if report.eligible else "VALIDATION_FAILED"
        if event:
            try:
                adapter = approved_adapter(
                    session, registry, source, FAMILY_TOOLS[family][0]
                )
                with adapter.catalogue.connector.connection(
                    consistent=True
                ) as connection:
                    runtime = evaluate_operational(
                        adapter, connection, diagnostics=False, fingerprint=False
                    )
                    runtime_invalid = runtime.invalid
                    if runtime.invalid:
                        raise SourceMappingError("capability_data_quality_changed")
                state = "ACTIVE"
                report = checked_report(
                    _record(session, source, family, event.report_id)
                )
            except SourceMappingError as exc:
                runtime_failure = exc.code
                state = "VALIDATION_FAILED"
                prerequisites.insert(0, exc.code)
        if failure:
            state = "SOURCE_UNAVAILABLE"
            prerequisites.insert(0, failure)
        states.append(
            CapabilityState(
                capability=family,
                status=state,
                connection_status=connection_status,
                mapping_status=report.structural_status
                if report
                else "MAPPING_REQUIRED",
                quality_status="BLOCKED_BY_DATA_QUALITY"
                if runtime_invalid
                else "UNVERIFIED"
                if runtime_failure
                else report.impact
                if report
                else "UNKNOWN",
                approval_status="REVOKED"
                if event and event.approval_type == "revoked"
                else "APPROVED"
                if event and event.expires_at > datetime.now(UTC)
                else "EXPIRED"
                if event
                else "UNAPPROVED",
                runtime_status="AVAILABLE" if state == "ACTIVE" else "UNAVAILABLE",
                runtime_invalid_records=runtime_invalid,
                prerequisites=tuple(prerequisites),
                candidates=candidates,
                report=report,
                latest_validation=latest_validation,
                approval_id=event.id if event else None,
                approval_version=event.version if event else None,
                expires_at=event.expires_at if event else None,
                tools=FAMILY_TOOLS[family] if state == "ACTIVE" else (),
                history=tuple(
                    {
                        "version": e.version,
                        "report_id": str(e.report_id),
                        "approval_type": e.approval_type,
                        "approved_at": e.created_at.isoformat(),
                        "expires_at": e.expires_at.isoformat(),
                    }
                    for e in session.scalars(
                        select(CatalogueQualityApproval)
                        .where(
                            CatalogueQualityApproval.source_id == source.id,
                            CatalogueQualityApproval.business_id == business_id,
                            CatalogueQualityApproval.capability == family,
                        )
                        .order_by(CatalogueQualityApproval.version.desc())
                        .limit(20)
                    )
                ),
            )
        )
    return CapabilityWorkspace(
        discovery=discovery,
        connection_status=connection_status,
        context={
            "business_id": str(business_id),
            "source_id": str(source_id),
            "catalogue_revision": revision.version if revision else 0,
            "source_fingerprint": discovery.source_fingerprint if discovery else None,
            "schema_fingerprint": discovery.schema_fingerprint if discovery else None,
        },
        identity_mapping=identity,
        access_inventory=access_inventory,
        capabilities=tuple(states),
        limitations=(
            "Readable metadata is evidence of access, not proof of business meaning.",
            "Each family is validated, approved, revoked and checked independently.",
            "No operational fact exclusions or automatic permission changes.",
        ),
    )
