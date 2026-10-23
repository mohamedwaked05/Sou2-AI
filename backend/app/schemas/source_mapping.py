"""Provider-neutral catalogue mappings. No field accepts executable SQL."""

import hashlib
import json
import uuid
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.schemas.owner_chat_state import ConversationContextKind, ProductPresentation


class MappingContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DiscoveredColumn(MappingContract):
    name: str = Field(min_length=1, max_length=128)
    kind: Literal["text", "integer", "decimal", "boolean", "date", "other"]
    nullable: bool
    type_signature: str | None = Field(default=None, max_length=512)


class DiscoveredObject(MappingContract):
    object_id: str = Field(pattern=r"^o[0-9]{1,3}$")
    schema_name: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    columns: tuple[DiscoveredColumn, ...] = Field(min_length=1, max_length=64)
    unique_keys: tuple[tuple[str, ...], ...] = Field(max_length=16)
    validation_stamp: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def consistent_metadata(self) -> DiscoveredObject:
        names = {column.name for column in self.columns}
        if len(names) != len(self.columns) or any(
            not key or len(set(key)) != len(key) or not set(key) <= names
            for key in self.unique_keys
        ):
            raise ValueError("Inconsistent catalogue metadata.")
        return self


class DiscoveredRelationship(MappingContract):
    source_object_id: str = Field(pattern=r"^o[0-9]{1,3}$")
    source_columns: tuple[str, ...] = Field(min_length=1, max_length=64)
    target_object_id: str = Field(pattern=r"^o[0-9]{1,3}$")
    target_columns: tuple[str, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def complete_pairs(self):
        if len(self.source_columns) != len(self.target_columns) or len(
            set(self.target_columns)
        ) != len(self.target_columns):
            raise ValueError("Relationship components must be complete, ordered pairs.")
        return self


class SchemaDiscovery(MappingContract):
    contract_version: Literal[1] = 1
    engine: Literal["postgresql", "sqlserver"]
    source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    objects: tuple[DiscoveredObject, ...] = Field(min_length=1, max_length=16)
    relationships: tuple[DiscoveredRelationship, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def bounded_metadata(self) -> SchemaDiscovery:
        if len({item.object_id for item in self.objects}) != len(self.objects):
            raise ValueError("Duplicate catalogue objects.")
        objects = {o.object_id: {c.name for c in o.columns} for o in self.objects}
        for relation in self.relationships:
            if not set(relation.source_columns) <= objects.get(
                relation.source_object_id, set()
            ) or not set(relation.target_columns) <= objects.get(
                relation.target_object_id, set()
            ):
                raise ValueError("Relationship columns are outside approved metadata.")
        if sum(len(item.columns) for item in self.objects) > 128:
            raise ValueError("Catalogue metadata exceeds the column bound.")
        if len(self.model_dump_json().encode("utf-8")) > 24_000:
            raise ValueError("Catalogue metadata exceeds the byte bound.")
        return self

    @property
    def schema_fingerprint(self) -> str:
        payload = self.model_dump(exclude={"source_fingerprint"}, mode="json")
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class JoinPair(MappingContract):
    product_column: str = Field(min_length=1, max_length=128)
    related_column: str = Field(min_length=1, max_length=128)


class CategoryMapping(MappingContract):
    object_id: str = Field(pattern=r"^o[0-9]{1,3}$")
    joins: tuple[JoinPair, ...] = Field(min_length=1, max_length=4)
    label_column: str = Field(min_length=1, max_length=128)


class IdentifierMapping(MappingContract):
    object_id: str = Field(pattern=r"^o[0-9]{1,3}$")
    joins: tuple[JoinPair, ...] = Field(min_length=1, max_length=4)
    value_column: str = Field(min_length=1, max_length=128)
    kind: Literal["barcode", "alternate_id"]


class ProductRowScope(MappingContract):
    column: str = Field(min_length=1, max_length=128)
    value: str | int


class ProductMapping(MappingContract):
    contract_version: Literal[1] = 1
    capability: Literal["products"]
    row_scopes: tuple[ProductRowScope, ...] = Field(default=(), max_length=4)
    object_id: str = Field(pattern=r"^o[0-9]{1,3}$")
    key_columns: tuple[str, ...] = Field(min_length=1, max_length=4)
    name_columns: tuple[str, ...] = Field(min_length=1, max_length=3)
    sku_column: str | None = Field(default=None, min_length=1, max_length=128)
    categories: tuple[CategoryMapping, ...] = Field(default=(), max_length=3)
    identifiers: tuple[IdentifierMapping, ...] = Field(default=(), max_length=3)


class MappingProposal(MappingContract):
    mapping: ProductMapping | None
    uncertainties: tuple[str, ...] = Field(max_length=12)
    # These are review notes, never instructions or query fragments.
    rationale: str = Field(min_length=1, max_length=1500)

    @model_validator(mode="after")
    def bounded_review_notes(self) -> MappingProposal:
        if any(not note.strip() or len(note) > 300 for note in self.uncertainties):
            raise ValueError("Invalid mapping uncertainty.")
        if self.mapping is None and not self.uncertainties:
            raise ValueError("An incomplete proposal must explain its uncertainty.")
        return self


class MappingProposeRequest(MappingContract):
    idempotency_key: uuid.UUID


class MappingApproveRequest(MappingContract):
    mapping: ProductMapping
    confirm_semantics: Literal[True]
    acknowledge_uncertainties: Literal[True]


class MappingReview(MappingContract):
    id: uuid.UUID
    version: int
    status: Literal["proposing", "review", "approved", "failed"]
    discovery: SchemaDiscovery
    proposal: MappingProposal | None
    approved_mapping: ProductMapping | None
    schema_fingerprint: str
    failure_code: str | None
    validation_notes: tuple[str, ...] = ()


class CatalogueRequest(MappingContract):
    browse: Literal[True] | None = None
    category: str | None = Field(default=None, min_length=1, max_length=255)
    continuation: str | None = Field(default=None, min_length=1, max_length=4096)
    query: str | None = Field(default=None, min_length=1, max_length=128)
    external_product_id: str | None = Field(default=None, min_length=1, max_length=4096)
    mapping_version: int | None = Field(default=None, ge=1)
    limit: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="after")
    def one_selector(self) -> CatalogueRequest:
        if (
            sum(
                (
                    self.query is not None,
                    self.external_product_id is not None,
                    bool(self.browse),
                )
            )
            != 1
        ):
            raise ValueError("Specify a search, browse, or an explicit identifier.")
        if self.external_product_id is not None and (
            self.continuation or self.category
        ):
            raise ValueError("Exact selection cannot include discovery filters.")
        if self.external_product_id is not None and self.mapping_version is None:
            raise ValueError("Explicit selection requires the offered mapping version.")
        return self


class CatalogueProduct(MappingContract):
    external_product_id: str
    names: tuple[str, ...]
    sku: str | None
    categories: tuple[str, ...]
    # Stock is deliberately absent from the mapping contract.
    stock: None = None


class CatalogueCoverage(MappingContract):
    approval_type: Literal["full", "partial"] = "full"
    approval_id: uuid.UUID | None = None
    approval_version: int | None = None
    excluded_records: int = 0
    total_records: int | None = None
    valid_records: int | None = None
    expires_at: AwareDatetime | None = None
    disclosure: str | None = None


class CatalogueResult(MappingContract):
    semantic_query: str | None = Field(default=None, max_length=255)
    filter_requests: tuple[str, ...] = Field(default=(), max_length=8)
    brand_labels: tuple[str, ...] = Field(default=(), max_length=20)
    matched_records: int | None = Field(default=None, ge=0)
    coverage: CatalogueCoverage | None = None
    deferred_operation: dict[str, object] | None = None
    pagination_scope: str | None = None
    key_size: int = Field(default=1, ge=1, le=4)
    discovery_request: CatalogueRequest | None = None
    continuation: str | None = None
    search_semantics: str = "literal"
    mapping_version: int = Field(ge=1)
    status: Literal["resolved", "ambiguous", "not_found"]
    items: tuple[CatalogueProduct, ...] = Field(max_length=50)
    truncated: bool
    capabilities: tuple[Literal["products"], ...] = ("products",)


class PendingCatalogueClarification(MappingContract):
    """Scoped source choices; the planner cannot create selection identifiers."""

    deferred_operation: dict[str, object] | None = None
    semantic_query: str | None = Field(default=None, max_length=255)
    filter_requests: tuple[str, ...] = Field(default=(), max_length=8)
    coverage: CatalogueCoverage | None = None
    continuation: str | None = None
    discovery_request: CatalogueRequest | None = None
    operation: Literal["product_search"] = "product_search"
    state_kind: ConversationContextKind = "clarification"
    presentation: ProductPresentation | None = None
    business_id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID
    source_id: uuid.UUID
    source_updated_at: AwareDatetime
    mapping_version: int = Field(ge=1)
    schema_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: AwareDatetime
    candidates: tuple[CatalogueProduct, ...] = Field(max_length=50)
    truncated: bool
    owner_request: str = Field(min_length=1, max_length=14_000)

    @model_validator(mode="after")
    def validate_choices(self) -> PendingCatalogueClarification:
        identifiers = [item.external_product_id for item in self.candidates]
        if (
            (not identifiers and self.state_kind == "clarification")
            or len(set(identifiers)) != len(identifiers)
            or (
                len(identifiers) < 2
                and not self.truncated
                and self.state_kind == "clarification"
            )
        ):
            raise ValueError("A catalogue clarification requires ambiguous choices.")
        return self
