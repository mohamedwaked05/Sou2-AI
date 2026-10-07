"""Provider-neutral catalogue mappings. No field accepts executable SQL."""

import hashlib
import json
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    source_object_id: str
    source_columns: tuple[str, ...]
    target_object_id: str
    target_columns: tuple[str, ...]


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


class ProductMapping(MappingContract):
    contract_version: Literal[1] = 1
    capability: Literal["products"] = "products"
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
    query: str | None = Field(default=None, min_length=1, max_length=128)
    external_product_id: str | None = Field(default=None, min_length=1, max_length=4096)
    mapping_version: int | None = Field(default=None, ge=1)
    limit: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="after")
    def one_selector(self) -> CatalogueRequest:
        if (self.query is None) == (self.external_product_id is None):
            raise ValueError("Specify a search or an explicit product identifier.")
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


class CatalogueResult(MappingContract):
    mapping_version: int = Field(ge=1)
    status: Literal["resolved", "ambiguous", "not_found"]
    items: tuple[CatalogueProduct, ...] = Field(max_length=50)
    truncated: bool
    capabilities: tuple[Literal["products"], ...] = ("products",)
