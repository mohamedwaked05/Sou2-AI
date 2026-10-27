"""Tenant-scoped exact quality reports and append-only explicit coverage approval."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.admission import require_open_admission
from app.core.config import get_settings
from app.database.models import (
    CatalogueQualityApproval,
    CatalogueQualityReportRecord,
    OperationalDataSourceConfig,
    OperationalDataSourceStatus,
    SourceMappingRevision,
    User,
)
from app.integrations.catalogue_quality import (
    RULE_EXPLANATIONS,
    evaluate_quality,
    mapping_digest,
    within_tolerance,
)
from app.integrations.discovery import SourceMappingError
from app.integrations.mapped_products import MappedProductSource
from app.integrations.profiles import ConnectionProfileRegistry
from app.schemas.catalogue_quality import (
    CatalogueCoverage,
    ExclusionPolicy,
    PartialApprovalRequest,
    QualityReport,
    QualityValidationRequest,
)
from app.schemas.source_mapping import MappingContract, ProductMapping, SchemaDiscovery
from app.services.source_mapping import (
    _source,
    connector_for,
    latest_approved,
    mapping_error,
)


def report_integrity(report: MappingContract) -> str:
    return hashlib.sha256(
        json.dumps(
            report.model_dump(mode="json", exclude={"integrity"}),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _serialize_validation(
    session: Session, source: OperationalDataSourceConfig
) -> None:
    key = int.from_bytes(
        hashlib.sha256(source.id.bytes).digest()[:8], "big", signed=True
    )
    if not session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}):
        raise mapping_error("catalogue_validation_in_progress")


def _revision(
    session: Session,
    source: OperationalDataSourceConfig,
    revision_id: uuid.UUID | None = None,
) -> SourceMappingRevision:
    if revision_id is None:
        revision = latest_approved(session, source)
    else:
        revision = session.scalar(
            select(SourceMappingRevision).where(
                SourceMappingRevision.id == revision_id,
                SourceMappingRevision.source_id == source.id,
                SourceMappingRevision.business_id == source.business_id,
            )
        )
    if revision is None:
        raise mapping_error("mapping_revision_not_found", 404)
    if revision.status not in {"review", "approved"}:
        raise mapping_error("mapping_review_required")
    return revision


def create_report(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    body: QualityValidationRequest,
    registry: ConnectionProfileRegistry,
) -> QualityReport:
    require_open_admission()
    source = _source(session, user, business_id, source_id)
    _serialize_validation(session, source)
    revision = _revision(session, source, body.revision_id)
    mapping = ProductMapping.model_validate(
        revision.approved_mapping
        or (
            body.mapping.model_dump(mode="json")
            if body.mapping
            else revision.proposal["mapping"]
        )
    )
    if (
        revision.status == "approved"
        and body.mapping is not None
        and body.mapping != mapping
    ):
        raise mapping_error("mapping_revision_immutable")
    expected_updated = source.updated_at
    discovery = SchemaDiscovery.model_validate(revision.discovery)
    connector = connector_for(registry, source)
    now = datetime.now(UTC)
    settings = get_settings()
    payload = dict(
        id=uuid.uuid4(),
        business_id=business_id,
        source_id=source_id,
        revision_id=revision.id,
        mapping_version=revision.version,
        mapping=mapping,
        schema_fingerprint=revision.schema_fingerprint,
        source_fingerprint=discovery.source_fingerprint,
        validated_at=now,
        expires_at=now
        + timedelta(seconds=settings.catalogue_quality_report_ttl_seconds),
        total_records=None,
        valid_records=None,
        invalid_records=None,
        invalid_percent=None,
        max_invalid_percent=settings.catalogue_max_invalid_percent,
        reason_counts={},
        structural_status="INCOMPLETE",
        impact="INSUFFICIENT_CAPABILITY_EVIDENCE",
        findings=(),
        eligible=False,
        recommended_action="Complete read-only validation before requesting approval.",
        exclusion_policy=None,
    )
    try:
        adapter = MappedProductSource(connector, discovery, mapping, revision.version)
        profile = registry.get_profile(source.connection_profile_key)
        if len(profile.business_ids) > 1 and not mapping.row_scopes:
            raise SourceMappingError("mapping_catalogue_partition_required")
        with connector.connection(consistent=True) as connection:
            result = evaluate_quality(adapter, connection)
        eligible = bool(
            result.invalid
            and result.relationship_digest is not None
            and within_tolerance(
                result.invalid, result.total, settings.catalogue_max_invalid_percent
            )
            and result.impact == "SAFE_WITH_EXCLUSIONS"
            and result.total > result.invalid
        )
        findings, impact = result.findings, result.impact
        payload.update(
            total_records=result.total,
            valid_records=result.total - result.invalid,
            invalid_records=result.invalid,
            invalid_percent=result.percent,
            reason_counts=result.reasons,
            relationship_counts=result.relationship_counts or {},
            structural_status="PASS",
            impact=impact,
            findings=findings,
            eligible=eligible,
            diagnostics=result.diagnostics,
            diagnostics_truncated=result.invalid > len(result.diagnostics),
            exclusion_policy=ExclusionPolicy(
                rules=tuple(RULE_EXPLANATIONS),
                mapping_digest=mapping_digest(mapping),
                data_digest=result.data_digest,
                relationship_digest=result.relationship_digest,
            ),
            recommended_action=(
                "Review and explicitly approve the exact catalogue exclusions."
            )
            if eligible
            else (
                "No exclusions are needed. Use normal full mapping "
                "approval and activation."
            )
            if not result.invalid
            else (
                "Correct source values or resolve the reported "
                "relationship/policy blockers, then revalidate."
            ),
        )
        if not within_tolerance(
            result.invalid, result.total, settings.catalogue_max_invalid_percent
        ):
            payload["findings"] += (
                "Invalid-record percentage exceeds the configured inclusive tolerance.",
            )
    except SourceMappingError as exc:
        structural = not exc.code.startswith(
            ("source_", "discovery_", "catalogue_validation_")
        )
        payload.update(
            structural_status="BLOCKED" if structural else "INCOMPLETE",
            impact="BLOCKED_BY_MAPPING"
            if structural
            else "INSUFFICIENT_CAPABILITY_EVIDENCE",
            findings=(exc.code,),
            recommended_action=(
                "Resolve the reported mapping or source-access error, "
                "then revalidate. Partial approval is blocked."
            ),
        )
    source = _source(session, user, business_id, source_id, lock=True)
    if source.updated_at != expected_updated:
        raise mapping_error("data_source_state_conflict")
    report = QualityReport(**payload)
    while len(report.model_dump_json().encode("utf-8")) > 90_000 and report.diagnostics:
        report = report.model_copy(
            update={
                "diagnostics": report.diagnostics[:-1],
                "diagnostics_truncated": True,
            }
        )
    report = report.model_copy(update={"integrity": report_integrity(report)})
    session.add(
        CatalogueQualityReportRecord(
            id=report.id,
            business_id=business_id,
            source_id=source_id,
            revision_id=revision.id,
            requested_by=user.id,
            mapping=mapping.model_dump(mode="json"),
            source_updated_at=expected_updated,
            report=report.model_dump(mode="json"),
        )
    )
    session.commit()
    return report


def get_report(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    registry: ConnectionProfileRegistry,
    report_id: uuid.UUID | None = None,
) -> QualityReport | None:
    source = _source(session, user, business_id, source_id)
    connector_for(registry, source)
    query = select(CatalogueQualityReportRecord).where(
        CatalogueQualityReportRecord.business_id == business_id,
        CatalogueQualityReportRecord.source_id == source_id,
        CatalogueQualityReportRecord.capability == "catalogue",
    )
    query = (
        query.where(CatalogueQualityReportRecord.id == report_id)
        if report_id
        else query.order_by(
            CatalogueQualityReportRecord.created_at.desc(),
            CatalogueQualityReportRecord.id.desc(),
        ).limit(1)
    )
    record = session.scalar(query)
    if record is None:
        return None
    try:
        report = QualityReport.model_validate(record.report)
        if report.integrity != report_integrity(report):
            raise ValueError
    except ValidationError, ValueError:
        raise mapping_error("catalogue_quality_report_invalid") from None
    return report


def latest_event(
    session: Session, source: OperationalDataSourceConfig
) -> CatalogueQualityApproval | None:
    return session.scalar(
        select(CatalogueQualityApproval)
        .where(
            CatalogueQualityApproval.source_id == source.id,
            CatalogueQualityApproval.business_id == source.business_id,
            CatalogueQualityApproval.capability == "catalogue",
        )
        .order_by(CatalogueQualityApproval.version.desc())
        .limit(1)
    )


def approved_coverage(
    session: Session,
    source: OperationalDataSourceConfig,
    revision: SourceMappingRevision,
) -> tuple[ExclusionPolicy | None, CatalogueCoverage | None]:
    event = latest_event(session, source)
    if event is None or event.approval_type == "full":
        return None, None
    record = session.get(CatalogueQualityReportRecord, event.report_id)
    if record.revision_id != revision.id:
        # A new independently approved full mapping never inherits old exclusions.
        return None, None
    if event.approval_type == "revoked":
        raise SourceMappingError("catalogue_partial_approval_revoked")
    report = QualityReport.model_validate(record.report)
    if (
        record.revision_id != revision.id
        or record.mapping != revision.approved_mapping
        or report.integrity != report_integrity(report)
        or not report.eligible
        or report.impact not in {"SAFE_WITH_EXCLUSIONS", "SAFE_FOR_PARTIAL_CATALOGUE"}
        or report.schema_fingerprint != revision.schema_fingerprint
        or report.max_invalid_percent > get_settings().catalogue_max_invalid_percent
        or not within_tolerance(
            report.invalid_records,
            report.total_records,
            get_settings().catalogue_max_invalid_percent,
        )
        or event.expires_at <= datetime.now(UTC)
    ):
        raise SourceMappingError("catalogue_partial_approval_stale")
    coverage = CatalogueCoverage(
        approval_type="partial",
        approval_id=event.id,
        approval_version=event.version,
        excluded_records=report.invalid_records,
        total_records=report.total_records,
        valid_records=report.valid_records,
        expires_at=event.expires_at,
        disclosure=(
            f"Validated partial catalogue: {report.invalid_records} invalid "
            "source records are excluded. Stock, prices and financial coverage "
            "are not approved by this policy."
        ),
    )
    return report.exclusion_policy, coverage


def _new_event(
    session: Session,
    source: OperationalDataSourceConfig,
    user: User,
    report_id: uuid.UUID,
    approval_type: str,
    expires_at: datetime,
    capability: str = "catalogue",
) -> CatalogueQualityApproval:
    version = (
        session.scalar(
            select(func.max(CatalogueQualityApproval.version)).where(
                CatalogueQualityApproval.source_id == source.id
            )
        )
        or 0
    ) + 1
    event = CatalogueQualityApproval(
        business_id=source.business_id,
        source_id=source.id,
        report_id=report_id,
        capability=capability,
        version=version,
        approval_type=approval_type,
        approved_by=user.id,
        expires_at=expires_at,
    )
    session.add(event)
    return event


def approve_partial(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    body: PartialApprovalRequest,
    registry: ConnectionProfileRegistry,
) -> CatalogueCoverage:
    require_open_admission()
    source = _source(session, user, business_id, source_id, lock=True)
    _serialize_validation(session, source)
    report = get_report(session, user, business_id, source_id, registry, body.report_id)
    if report is None:
        raise mapping_error("catalogue_quality_report_not_found", 404)
    record = session.get(CatalogueQualityReportRecord, report.id)
    previous = session.scalar(
        select(CatalogueQualityApproval).where(
            CatalogueQualityApproval.report_id == report.id,
            CatalogueQualityApproval.approval_type == body.approval_type,
        )
    )
    if previous is not None:
        if latest_event(session, source).id != previous.id:
            raise mapping_error("catalogue_partial_approval_revoked")
        # A retry must carry exactly the same explicit confirmation.
    if (
        not hmac.compare_digest(report.integrity, body.integrity)
        or not (
            report.eligible
            if body.approval_type == "partial"
            else report.structural_status == "PASS" and report.invalid_records == 0
        )
        or body.excluded_records != report.invalid_records
        or body.total_records != report.total_records
    ):
        raise mapping_error("catalogue_quality_confirmation_invalid")
    revision = _revision(session, source, report.revision_id)
    latest = latest_approved(session, source)
    if (
        report.expires_at <= datetime.now(UTC)
        or source.updated_at != record.source_updated_at
        or report.max_invalid_percent != get_settings().catalogue_max_invalid_percent
        or (latest is not None and latest.version > revision.version)
    ) and previous is None:
        raise mapping_error("catalogue_quality_report_stale")
    if previous is not None:
        try:
            policy, coverage = approved_coverage(session, source, revision)
        except SourceMappingError:
            raise mapping_error("catalogue_quality_report_stale") from None
        if body.approval_type == "partial" and policy is None:
            raise mapping_error("catalogue_quality_report_stale")
        session.commit()
        return coverage or CatalogueCoverage(
            total_records=report.total_records, valid_records=report.valid_records
        )
    mapping = ProductMapping.model_validate(record.mapping)
    if revision.status == "approved" and revision.approved_mapping != record.mapping:
        raise mapping_error("mapping_revision_immutable")
    active_renewal = source.status is OperationalDataSourceStatus.ACTIVE
    if active_renewal:
        event = latest_event(session, source)
        prior = (
            session.get(CatalogueQualityReportRecord, event.report_id)
            if event
            else None
        )
        if (
            revision.status != "approved"
            or latest is None
            or latest.id != revision.id
            or event is None
            or event.approval_type not in {"full", "partial"}
            or prior is None
            or prior.revision_id != revision.id
            or prior.mapping != record.mapping
        ):
            raise mapping_error("mapping_disable_before_approval")
    connector = connector_for(registry, source)
    discovery = SchemaDiscovery.model_validate(revision.discovery)
    if (
        report.schema_fingerprint != revision.schema_fingerprint
        or report.source_fingerprint != discovery.source_fingerprint
        or report.exclusion_policy is None
        or report.exclusion_policy.mapping_digest != mapping_digest(mapping)
    ):
        raise mapping_error("catalogue_quality_report_stale")
    try:
        adapter = MappedProductSource(connector, discovery, mapping, revision.version)
        # Hold the consistent external read through the platform approval commit.
        # Runtime reads independently enforce the identical snapshot digest.
        with connector.connection(consistent=True) as connection:
            current = evaluate_quality(adapter, connection, diagnostics=False)
            if (
                current.data_digest != report.exclusion_policy.data_digest
                or current.relationship_digest
                != report.exclusion_policy.relationship_digest
                or current.total != report.total_records
                or current.invalid != report.invalid_records
                or (
                    body.approval_type == "partial"
                    and current.impact != "SAFE_WITH_EXCLUSIONS"
                )
            ):
                raise SourceMappingError("catalogue_quality_report_stale")
            if revision.status == "review":
                revision.status = "approved"
                revision.approved_mapping = record.mapping
                revision.approved_by = user.id
                revision.approved_at = datetime.now(UTC)
                revision.validation_notes = [
                    f"Explicit {body.approval_type} catalogue approval; "
                    "see immutable quality evidence."
                ]
                session.flush()
            _new_event(
                session,
                source,
                user,
                report.id,
                body.approval_type,
                datetime.now(UTC)
                + timedelta(
                    seconds=get_settings().catalogue_partial_approval_ttl_seconds
                ),
            )
            source.status = (
                OperationalDataSourceStatus.ACTIVE
                if active_renewal
                else OperationalDataSourceStatus.VALIDATED
            )
            source.last_validated_at = datetime.now(UTC)
            source.last_successful_health_check_at = source.last_validated_at
            source.failure_code = None
            session.flush()
            policy, coverage = approved_coverage(session, source, revision)
            session.commit()
    except SourceMappingError as exc:
        raise mapping_error(exc.code) from None
    return coverage or CatalogueCoverage(
        total_records=report.total_records, valid_records=report.valid_records
    )


def revoke_for_source(
    session: Session,
    source: OperationalDataSourceConfig,
    user: User,
    *,
    include_operations=True,
) -> None:
    event = latest_event(session, source)
    if event is not None and event.approval_type == "partial":
        _new_event(session, source, user, event.report_id, "revoked", datetime.now(UTC))
    if not include_operations:
        return
    from app.services.capabilities import FAMILIES
    from app.services.capabilities import latest_event as capability_event

    for family in FAMILIES:
        event = capability_event(session, source, family)
        if event is not None and event.approval_type != "revoked":
            _new_event(
                session,
                source,
                user,
                event.report_id,
                "revoked",
                datetime.now(UTC),
                family,
            )


def revoke_partial(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    source_id: uuid.UUID,
    registry: ConnectionProfileRegistry,
) -> None:
    require_open_admission()
    source = _source(session, user, business_id, source_id, lock=True)
    connector_for(registry, source)
    revoke_for_source(session, source, user, include_operations=False)
    source.status = OperationalDataSourceStatus.UNHEALTHY
    source.failure_code = "catalogue_partial_approval_revoked"
    source.last_validated_at = datetime.now(UTC)
    session.commit()
