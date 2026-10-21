"""Presentation contracts retain validated source facts and source authority."""

import uuid
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from app.schemas.operational import (
    BestSellingProductsResult,
    CategoryResolution,
    InventoryResult,
    LocationResolution,
    MetricCapabilityResult,
    ProductDiscoveryResult,
    ProductResolution,
    RestockingRecommendationsResult,
    SalesSummary,
)
from app.schemas.operational_mapping import OperationalReport
from app.schemas.owner_chat_state import ProductPresentation
from app.schemas.source_mapping import CatalogueResult

PresentationResult = (
    OperationalReport
    | CatalogueResult
    | InventoryResult
    | SalesSummary
    | BestSellingProductsResult
    | RestockingRecommendationsResult
    | MetricCapabilityResult
    | ProductResolution
    | CategoryResolution
    | LocationResolution
)


class PresentationProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: uuid.UUID | None = None
    source_updated_at: AwareDatetime | None = None
    mapping_version: int | None = None
    schema_fingerprint: str | None = None
    source_fingerprint: str | None = None


class OperationalPresentation(BaseModel):
    """An immutable view; quantities, periods, units and metadata stay in result.

    Source metadata contains freshness and bounded row counts. No flattening or
    deduplication may erase distinct product records or per-location stock rows.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    capability: Literal[
        "mapped_operations",
        "products",
        "inventory",
        "sales",
        "best_sellers",
        "restocking",
        "financial_metric",
        "product_choice",
        "category_choice",
        "location_choice",
    ]
    operation: str | None = None
    presentation_kind: Literal["list", "detail", "clarification", "report"] = "list"
    purpose: ProductPresentation | None = None
    identifiers: tuple[Literal["id", "sku"], ...] = ()
    language: Literal["english", "arabic"] = "english"
    result: PresentationResult
    source_records: ProductDiscoveryResult | CatalogueResult | None = None
    provenance: PresentationProvenance = PresentationProvenance()
    unknown_fields: tuple[str, ...] = ()
    unsupported_fields: tuple[str, ...] = ()
    ambiguous: bool = False
    truncated: bool = False
    # Continuations are source-bound navigation, never authorization.
    pagination_available: bool = False
