"""Capability reports use the existing revision-19 quality/approval ledger."""

import uuid
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.schemas.operational_mapping import OperationalMapping
from app.schemas.source_mapping import MappingContract, ProductMapping, SchemaDiscovery

Capability = Literal["inventory", "pricing", "sales", "financial"]
Classification = Literal[
    "SAFE_FOR_CAPABILITY",
    "SAFE_WITH_EXCLUSIONS",
    "BLOCKED_BY_MAPPING",
    "BLOCKED_BY_DATA_QUALITY",
    "INSUFFICIENT_CAPABILITY_EVIDENCE",
]


class CapabilityValidation(MappingContract):
    mapping: OperationalMapping
    discovery: SchemaDiscovery
    meaning: str = Field(min_length=12, max_length=2000)


class CapabilityDiagnostic(MappingContract):
    object_id: str
    identifier: tuple[str | None, ...]
    rules: tuple[str, ...]
    explanation: str
    safe_exclusion: bool = False
    locator_note: str = (
        "Bounded original fact-key values; never a replacement identity."
    )


class CapabilityQualityReport(MappingContract):
    contract_version: Literal[1] = 1
    id: uuid.UUID
    business_id: uuid.UUID
    source_id: uuid.UUID
    revision_id: uuid.UUID | None
    capability: Capability
    mapping: OperationalMapping
    discovery: SchemaDiscovery
    identity_mapping: ProductMapping | None
    meaning: str
    source_fingerprint: str
    schema_fingerprint: str
    validated_at: AwareDatetime
    expires_at: AwareDatetime
    total_records: int | None = None
    valid_records: int | None = None
    invalid_records: int | None = None
    invalid_percent: Decimal | None = None
    max_invalid_percent: Decimal
    denominator: str = (
        "All in-scope fact records, including explicitly classified"
        " non-completed statuses."
    )
    reason_counts: dict[str, int] = Field(default_factory=dict)
    record_counts: dict[str, int] = Field(default_factory=dict)
    structural_status: Literal["PASS", "BLOCKED", "INCOMPLETE"]
    impact: Classification
    eligible: bool = False
    exclusions_permitted: Literal[False] = False
    findings: tuple[str, ...]
    diagnostics: tuple[CapabilityDiagnostic, ...] = Field(default=(), max_length=50)
    diagnostics_truncated: bool = False
    data_digest: str | None = None
    integrity: str = ""

    @model_validator(mode="after")
    def consistent_evidence(self):
        if self.expires_at <= self.validated_at:
            raise ValueError("Capability freshness interval is invalid.")
        if (
            self.mapping.business_id != self.business_id
            or self.mapping.source_id != self.source_id
            or self.mapping.schema_fingerprint != self.discovery.schema_fingerprint
            or self.schema_fingerprint != self.discovery.schema_fingerprint
            or self.source_fingerprint != self.discovery.source_fingerprint
        ):
            raise ValueError("Capability scope evidence differs.")
        if self.total_records is not None and (
            self.invalid_records is None
            or self.valid_records is None
            or min(self.total_records, self.invalid_records, self.valid_records) < 0
            or self.valid_records + self.invalid_records != self.total_records
            or any(
                c < 0 or c > self.invalid_records for c in self.reason_counts.values()
            )
        ):
            raise ValueError("Capability record counts are inconsistent.")
        if self.eligible and (
            self.invalid_records != 0
            or self.structural_status != "PASS"
            or self.impact != "SAFE_FOR_CAPABILITY"
            or not self.data_digest
        ):
            raise ValueError("Operational approval requires complete valid facts.")
        return self


class CapabilityApproval(MappingContract):
    report_id: uuid.UUID
    integrity: str = Field(pattern=r"^[a-f0-9]{64}$")
    total_records: int = Field(ge=0)
    excluded_records: Literal[0] = 0
    confirm_semantics: Literal[True]
    confirm_scope: Literal[True]
    confirm_enable_capability: Literal[True]


class CapabilityActivation(MappingContract):
    approval_id: uuid.UUID
    version: int = Field(ge=1)
    status: Literal["ACTIVE"] = "ACTIVE"
    capability: Capability


class CapabilityState(MappingContract):
    capability: Literal["catalogue", "inventory", "pricing", "sales", "financial"]
    status: Literal[
        "DISCOVERED",
        "MAPPING_REQUIRED",
        "MAPPING_IN_REVIEW",
        "VALIDATION_FAILED",
        "VALIDATED",
        "APPROVED",
        "ACTIVE",
        "UNSUPPORTED",
        "SOURCE_UNAVAILABLE",
    ]
    connection_status: str
    mapping_status: str
    quality_status: str
    approval_status: str
    runtime_status: str
    runtime_invalid_records: int | None = None
    prerequisites: tuple[str, ...] = ()
    candidates: tuple[dict, ...] = ()
    report: CapabilityQualityReport | None = None
    latest_validation: CapabilityQualityReport | None = None
    approval_id: uuid.UUID | None = None
    approval_version: int | None = None
    expires_at: AwareDatetime | None = None
    tools: tuple[str, ...] = ()
    history: tuple[dict, ...] = Field(default=(), max_length=20)


class CapabilityWorkspace(MappingContract):
    discovery: SchemaDiscovery | None
    connection_status: str
    context: dict
    identity_mapping: ProductMapping | None
    access_inventory: dict = Field(default_factory=dict)
    capabilities: tuple[CapabilityState, ...]
    limitations: tuple[str, ...]
