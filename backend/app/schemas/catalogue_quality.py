"""Versioned, owner-visible catalogue quality and explicit approval contracts."""

import uuid
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.schemas.source_mapping import CatalogueCoverage as CatalogueCoverage
from app.schemas.source_mapping import MappingContract, ProductMapping

Impact = Literal[
    "SAFE_FOR_PARTIAL_CATALOGUE",
    "REQUIRES_OWNER_REVIEW",
    "UNSAFE",
    "UNKNOWN_IMPACT",
    "SAFE_FOR_CAPABILITY",
    "SAFE_WITH_EXCLUSIONS",
    "BLOCKED_BY_MAPPING",
    "BLOCKED_BY_DATA_QUALITY",
    "INSUFFICIENT_CAPABILITY_EVIDENCE",
]


class QualityValidationRequest(MappingContract):
    revision_id: uuid.UUID | None = None
    mapping: ProductMapping | None = None


class InvalidCatalogueRecord(MappingContract):
    identifier: str | None = Field(max_length=128)
    name: str | None = Field(max_length=255)
    rules: tuple[str, ...]
    explanation: str
    relationship_concerns: tuple[str, ...]
    safe_exclusion: bool
    severity: Literal["warning", "blocked"]
    suggested_action: str
    locator_note: str = (
        "Only approved identifier/name fields are shown. Display values may be "
        "truncated; composite values use mapping order. This diagnostic display "
        "is not a stable database locator and never supplies a replacement key."
    )


class ExclusionPolicy(MappingContract):
    version: Literal[1, 2] = 2
    kind: Literal["invalid_required_catalogue_values"] = (
        "invalid_required_catalogue_values"
    )
    rules: tuple[str, ...]
    mapping_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    data_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    relationship_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class QualityReport(MappingContract):
    contract_version: Literal[1] = 1
    capability: Literal["catalogue"] = "catalogue"
    id: uuid.UUID
    business_id: uuid.UUID
    source_id: uuid.UUID
    revision_id: uuid.UUID
    mapping_version: int
    mapping: ProductMapping
    schema_fingerprint: str
    source_fingerprint: str
    validated_at: AwareDatetime
    expires_at: AwareDatetime
    total_records: int | None
    valid_records: int | None
    invalid_records: int | None
    invalid_percent: Decimal | None
    max_invalid_percent: Decimal
    boundary_rule: Literal["inclusive"] = "inclusive"
    reason_counts: dict[str, int]
    relationship_counts: dict[str, int] = Field(default_factory=dict)
    structural_status: Literal["PASS", "BLOCKED", "INCOMPLETE"]
    impact: Impact
    findings: tuple[str, ...]
    eligible: bool
    recommended_action: str
    exclusion_policy: ExclusionPolicy | None
    diagnostics: tuple[InvalidCatalogueRecord, ...] = Field(max_length=50)
    diagnostics_truncated: bool = False
    integrity: str = ""

    @model_validator(mode="after")
    def consistent_evidence(self):
        if self.expires_at <= self.validated_at:
            raise ValueError("Report freshness interval is invalid.")
        if self.total_records is not None:
            if (
                self.valid_records is None
                or self.invalid_records is None
                or min(self.total_records, self.valid_records, self.invalid_records) < 0
                or self.valid_records + self.invalid_records != self.total_records
                or any(
                    count < 0 or count > self.invalid_records
                    for count in self.reason_counts.values()
                )
            ):
                raise ValueError("Catalogue quality counts are inconsistent.")
        if self.eligible and (
            self.structural_status != "PASS"
            or self.impact not in {"SAFE_WITH_EXCLUSIONS", "SAFE_FOR_PARTIAL_CATALOGUE"}
            or not self.invalid_records
            or not self.valid_records
            or self.exclusion_policy is None
            or self.exclusion_policy.relationship_digest is None
        ):
            raise ValueError("Partial eligibility requires complete safety evidence.")
        return self


class PartialApprovalRequest(MappingContract):
    approval_type: Literal["full", "partial"] = "partial"
    report_id: uuid.UUID
    integrity: str = Field(pattern=r"^[a-f0-9]{64}$")
    excluded_records: int = Field(ge=0)
    total_records: int = Field(ge=1)
    confirm_semantics: Literal[True]
    acknowledge_uncertainties: Literal[True]
    confirm_catalogue_only: Literal[True]
