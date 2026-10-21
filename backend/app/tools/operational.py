"""Provider-neutral registry and executor for approved operational tools."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.exceptions import ApplicationError
from app.database.models import (
    BusinessStatus,
    OperationalDataSourceConfig,
    OperationalDataSourceStatus,
    ToolCallLog,
    ToolCallStatus,
    User,
)
from app.integrations.discovery import SourceConnection, SourceMappingError
from app.integrations.mapped_operational import MappedOperationalSource
from app.integrations.mapped_products import MappedProductSource
from app.integrations.operational import (
    OperationalDataInvalid,
    OperationalDataSource,
    OperationalIntegrationError,
    OperationalQueryTimeout,
    OperationalSourceUnavailable,
)
from app.integrations.profiles import ConnectionProfileRegistry, MappingProfileError
from app.schemas.operational import (
    BestSellersQuery,
    BestSellingProductsResult,
    CategoryCandidate,
    InventoryQuery,
    InventoryReadQuery,
    InventoryResult,
    LocationCandidate,
    LocationResolution,
    LocationResolutionQuery,
    MetricCapabilityResult,
    OperationalMetric,
    ProductDiscoveryResult,
    ProductResolution,
    ProductResolutionQuery,
    RestockingQuery,
    RestockingReadQuery,
    RestockingRecommendationsResult,
    SalesQuery,
    SalesSummary,
)
from app.schemas.operational_mapping import (
    OperationalPlannerRead,
    OperationalRead,
    OperationalReport,
    OperationalToolRead,
)
from app.schemas.source_mapping import (
    CatalogueProduct,
    CatalogueRequest,
    CatalogueResult,
)
from app.services.businesses import load_full_access_business
from app.tools.catalogue import execute_product_search
from app.utils.argument_hashing import hash_tool_arguments

_logger = logging.getLogger(__name__)

CURRENT_INVENTORY_TOOL = "current_inventory"
SALES_SUMMARY_TOOL = "sales_summary"
BEST_SELLING_PRODUCTS_TOOL = "best_selling_products"
RESTOCKING_RECOMMENDATIONS_TOOL = "restocking_recommendations"
PRODUCT_SEARCH_TOOL = "product_search"
UNKNOWN_TOOL_AUDIT_NAME = "unknown_tool"

MAX_TOOL_RESULT_ROWS = 50
MAX_BEST_SELLER_RESULTS = 20
MAX_CATEGORY_CANDIDATES = 50

SAFE_TOOL_ERROR_CODES = frozenset(
    {
        "unknown_tool",
        "invalid_arguments",
        "authorization_denied",
        "inactive_business",
        "integration_unavailable",
        "capability_unavailable",
        "timeout",
        "result_limit",
        "adapter_failure",
        "loop_limit",
        "audit_unavailable",
        "provider_failure",
    }
)

_CONTROL_PAYLOAD = re.compile(
    r"(?:https?://|postgres(?:ql)?://|\b(?:select|insert|update|delete|drop|alter|"
    r"truncate|create)\b\s|\b(?:password|passwd|secret|api[_ -]?key|"
    r"database[_ -]?url)\b|(?:```|<script|\$\{|;\s*--))",
    re.IGNORECASE,
)


class CurrentInventoryToolInput(InventoryQuery):
    limit: int = Field(default=50, ge=1, le=MAX_TOOL_RESULT_ROWS)


class CurrentInventoryPlannerInput(BaseModel):
    """Untrusted planner input; source identifiers remain a backend concern."""

    model_config = ConfigDict(extra="forbid")

    product_filter: str | None = Field(default=None, min_length=1, max_length=80)
    category_filter: str | None = Field(default=None, min_length=1, max_length=128)
    location_reference: str | None = Field(default=None, min_length=1, max_length=255)
    limit: int = Field(default=50, ge=1, le=MAX_TOOL_RESULT_ROWS)

    @field_validator("product_filter", "category_filter", "location_reference")
    @classmethod
    def strip_filter(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Operational filters cannot be blank.")
        return normalized


class ProductSearchPlannerInput(CatalogueRequest):
    """Planner discovery selectors; source IDs/cursors are backend-only."""

    external_product_id: None = None
    mapping_version: None = None
    continuation: None = None

    semantic_query: str | None = Field(default=None, min_length=1, max_length=255)
    search_queries: tuple[str, ...] = Field(default=(), max_length=6)


class BestSellingProductsToolInput(BestSellersQuery):
    limit: int = Field(default=10, ge=1, le=MAX_BEST_SELLER_RESULTS)


class SalesSummaryPlannerInput(BaseModel):
    """Interpretation only: the backend supplies and validates reporting bounds."""

    model_config = ConfigDict(extra="forbid")
    start_date: date | None = None
    end_date: date | None = None
    metric: OperationalMetric | None = None
    date_range: Literal["previous_completed_month"] | None = None
    use_pending_clarification: bool = False
    branch_external_id: str | None = Field(default=None, min_length=1, max_length=128)


class RestockingRecommendationsToolInput(RestockingQuery):
    limit: int = Field(default=50, ge=1, le=MAX_TOOL_RESULT_ROWS)


class ToolExecutionError(Exception):
    """Safe, normalized failure from the centralized executor."""

    def __init__(self, code: str, *, reason: str | None = None) -> None:
        if code not in SAFE_TOOL_ERROR_CODES:
            code = "adapter_failure"
        self.code = code
        self.reason = reason
        super().__init__(code)


ToolExecutor = Callable[
    [OperationalDataSource | MappedProductSource, BaseModel], BaseModel
]


@dataclass(frozen=True)
class OperationalToolDefinition:
    """One immutable allowlisted tool definition."""

    name: str
    description: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel] | tuple[type[BaseModel], ...]
    capability: str
    result_limit: int
    timeout_seconds: int
    executor: ToolExecutor
    provider_input_schema: type[BaseModel] | None = None
    approved_operations: tuple[str, ...] | None = None

    def provider_schema(self) -> dict[str, Any]:
        schema = (self.provider_input_schema or self.input_schema).model_json_schema()
        if self.approved_operations is not None:
            schema["properties"]["capability"]["enum"] = list(self.approved_operations)
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": schema,
        }


@dataclass(frozen=True)
class OperationalToolResult:
    tool_name: str
    output: BaseModel
    latency_ms: int
    source_id: uuid.UUID | None = None
    source_updated_at: datetime | None = None
    schema_fingerprint: str | None = None
    source_fingerprint: str | None = None
    source_result: BaseModel | None = None
    mapping_version: int | None = None


@dataclass(frozen=True)
class CatalogueSourceScope:
    mapping_version: int
    schema_fingerprint: str
    source_fingerprint: str


def _mapped_operational_report(source, query):
    assert isinstance(source, MappedOperationalSource)
    assert isinstance(query, OperationalToolRead)
    product_id = query.product_id
    if product_id is not None and (
        query.reference != "selected"
        or query.catalogue_revision != source.mapping.catalogue_revision
    ):
        raise SourceMappingError("mapping_selection_stale")
    if query.reference == "selected" and product_id is None:
        raise SourceMappingError("product_selection_required")
    if query.product_identity is not None:
        from app.integrations.mapped_products import encode_product_key

        if product_id is not None or query.product_query is not None:
            raise SourceMappingError("invalid_product_identifier")
        product_id = encode_product_key(query.product_identity)
    if query.product_query is not None:
        catalogue = (
            source.resolution_catalogue
            if getattr(source, "independent", False)
            else source.catalogue
        )
        if catalogue is None:
            raise SourceMappingError("catalogue_resolution_unapproved")
        found = catalogue.search(CatalogueRequest(query=query.product_query))
        if found.status != "resolved":
            deferred = OperationalPlannerRead.model_validate(
                query.model_dump(exclude={"product_id", "catalogue_revision"})
            ).model_dump(mode="json", exclude_none=True)
            return found.model_copy(update={"deferred_operation": deferred})
        product_id = found.items[0].external_product_id
    if query.capability == "prices" and product_id is None:
        raise SourceMappingError("product_selection_required")
    fields = query.model_dump(
        exclude={
            "product_query",
            "product_identity",
            "reference",
            "product_id",
            "catalogue_revision",
        }
    )
    return source.read(
        OperationalRead(
            **fields,
            product_id=product_id,
            catalogue_revision=source.mapping.catalogue_revision,
            mapping_revision=source.revision,
            as_of=datetime.now(UTC) if query.capability == "prices" else None,
        )
    )


def _product_search(
    source: MappedProductSource | OperationalDataSource, query: BaseModel
) -> BaseModel:
    assert isinstance(query, CatalogueRequest)
    if isinstance(source, MappedProductSource):
        return execute_product_search(source, query)
    return source.search_products(query)


def _inventory(source: OperationalDataSource, query: BaseModel) -> BaseModel:
    assert isinstance(query, InventoryQuery)
    resolution = _resolve_product_filter(source, query.product_filter)
    if resolution is not None and resolution.status != "resolved":
        return InventoryResult(
            items=(), metadata=resolution.metadata, resolution=resolution
        )
    category_resolution = _resolve_category_filter(source, query.category_filter)
    if category_resolution is not None and category_resolution.status != "resolved":
        return InventoryResult(
            items=(),
            metadata=category_resolution.metadata,
            resolution=resolution,
            category_resolution=category_resolution,
        )
    read_query = InventoryReadQuery(
        external_product_id=(
            resolution.product.external_product_id
            if resolution is not None and resolution.product is not None
            else None
        ),
        category_filter=(
            category_resolution.category.label
            if category_resolution is not None
            and category_resolution.category is not None
            else None
        ),
        branch_external_id=query.branch_external_id,
        warehouse_external_id=query.warehouse_external_id,
        limit=query.limit,
    )
    result = source.get_current_inventory(read_query)
    return result.model_copy(
        update={"resolution": resolution, "category_resolution": category_resolution}
    )


def _sales_summary(source: OperationalDataSource, query: BaseModel) -> BaseModel:
    assert isinstance(query, SalesQuery)
    return source.get_sales_summary(query)


def _unsupported_metric_result(
    query: SalesQuery, supported_metrics: tuple[str, ...], source_timezone: str
) -> MetricCapabilityResult:
    if query.metric == "gross_profit":
        missing: tuple[Literal["cost_cogs", "expenses", "valuation_basis"], ...] = (
            "cost_cogs",
        )
    elif query.metric == "net_profit":
        missing = ("cost_cogs", "expenses")
    else:
        missing = ("valuation_basis",)
    return MetricCapabilityResult(
        requested_metric=query.metric,
        status="unsupported",
        missing_inputs=missing,
        supported_metrics=tuple(
            metric
            for metric in supported_metrics
            if metric
            in {
                "revenue",
                "gross_profit",
                "net_profit",
                "sales_count",
                "inventory_value",
            }
        ),
        period=query.period(source_timezone),
        branch_external_id=query.branch_external_id,
    )


def _best_sellers(source: OperationalDataSource, query: BaseModel) -> BaseModel:
    assert isinstance(query, BestSellersQuery)
    return source.get_best_selling_products(query)


def _restocking(source: OperationalDataSource, query: BaseModel) -> BaseModel:
    assert isinstance(query, RestockingQuery)
    resolution = _resolve_product_filter(source, query.product_filter)
    if resolution is not None and resolution.status != "resolved":
        return RestockingRecommendationsResult(
            items=(), metadata=resolution.metadata, resolution=resolution
        )
    category_resolution = _resolve_category_filter(source, query.category_filter)
    if category_resolution is not None and category_resolution.status != "resolved":
        return RestockingRecommendationsResult(
            items=(),
            metadata=category_resolution.metadata,
            resolution=resolution,
            category_resolution=category_resolution,
        )
    read_query = RestockingReadQuery(
        external_product_id=(
            resolution.product.external_product_id
            if resolution is not None and resolution.product is not None
            else None
        ),
        category_filter=(
            category_resolution.category.label
            if category_resolution is not None
            and category_resolution.category is not None
            else None
        ),
        branch_external_id=query.branch_external_id,
        warehouse_external_id=query.warehouse_external_id,
        limit=query.limit,
    )
    result = source.get_restocking_recommendations(read_query)
    return result.model_copy(
        update={
            "resolution": resolution,
            "category_resolution": category_resolution,
        }
    )


def _resolve_product_filter(
    source: OperationalDataSource, product_filter: str | None
) -> ProductResolution | None:
    if product_filter is None:
        return None
    return source.resolve_product(ProductResolutionQuery(reference=product_filter))


def _resolve_category_filter(
    source: OperationalDataSource, category_filter: str | None
):
    if category_filter is None:
        return None
    return source.resolve_category(ProductResolutionQuery(reference=category_filter))


def build_operational_tool_registry(
    *,
    timeout_seconds: int,
) -> Mapping[str, OperationalToolDefinition]:
    """Build the fixed registry with deployment-configured query timeouts."""

    definitions = (
        OperationalToolDefinition(
            name="operational_report",
            description=(
                "Read approved mapped stock, availability, current prices, sales, "
                "revenue, refunds, best sellers or restocking. Use explicit aware "
                "start/end instants for reports. Product names go in product_query, "
                "never an invented identifier. Missing semantics are unsupported. "
                "No profit or cash-flow inference."
            ),
            input_schema=OperationalToolRead,
            provider_input_schema=OperationalPlannerRead,
            output_schema=(OperationalReport, CatalogueResult),
            capability="mapped_operations",
            result_limit=MAX_TOOL_RESULT_ROWS,
            timeout_seconds=timeout_seconds,
            executor=_mapped_operational_report,
        ),
        OperationalToolDefinition(
            name=PRODUCT_SEARCH_TOOL,
            description=(
                "Search the approved product catalogue by name, key, SKU or alternate "
                "identifier. Offer actual variants when ambiguous. Catalogue details "
                "only; stock, prices, sales and inferred aliases are unknown."
            ),
            input_schema=CatalogueRequest,
            provider_input_schema=ProductSearchPlannerInput,
            output_schema=CatalogueResult,
            capability="products",
            result_limit=MAX_TOOL_RESULT_ROWS,
            timeout_seconds=timeout_seconds,
            executor=_product_search,
        ),
        OperationalToolDefinition(
            name=CURRENT_INVENTORY_TOOL,
            description=(
                "Current inventory, quantities, reservations and availability; "
                "filter by "
                "ID, SKU, barcode, name, alias, source-resolved category and branch/"
                "warehouse. Product resolution: resolved, ambiguous or not_found."
            ),
            input_schema=CurrentInventoryToolInput,
            output_schema=InventoryResult,
            capability="inventory",
            result_limit=MAX_TOOL_RESULT_ROWS,
            timeout_seconds=timeout_seconds,
            executor=_inventory,
            provider_input_schema=CurrentInventoryPlannerInput,
        ),
        OperationalToolDefinition(
            name=SALES_SUMMARY_TOOL,
            description=(
                "Completed sales and finalized returns/refunds for bounded "
                "source-local "
                "dates and optional branch. All financial metrics use this tool; "
                "backend validates support and reports missing inputs."
            ),
            input_schema=SalesQuery,
            provider_input_schema=SalesSummaryPlannerInput,
            output_schema=(SalesSummary, MetricCapabilityResult),
            capability="sales_summaries",
            result_limit=1,
            timeout_seconds=timeout_seconds,
            executor=_sales_summary,
        ),
        OperationalToolDefinition(
            name=BEST_SELLING_PRODUCTS_TOOL,
            description=(
                "Best sellers by net quantity for bounded source-local dates and "
                "optional branch."
            ),
            input_schema=BestSellingProductsToolInput,
            output_schema=BestSellingProductsResult,
            capability="best_sellers",
            result_limit=MAX_BEST_SELLER_RESULTS,
            timeout_seconds=timeout_seconds,
            executor=_best_sellers,
        ),
        OperationalToolDefinition(
            name=RESTOCKING_RECOMMENDATIONS_TOOL,
            description=(
                "Deterministic replenishment from available stock, reorder points and "
                "target stock; filter by ID, SKU, barcode, name, alias or "
                "source-resolved category. Product resolution: resolved, ambiguous "
                "or not_found."
            ),
            input_schema=RestockingRecommendationsToolInput,
            output_schema=RestockingRecommendationsResult,
            capability="restocking_recommendations",
            result_limit=MAX_TOOL_RESULT_ROWS,
            timeout_seconds=timeout_seconds,
            executor=_restocking,
        ),
    )
    return MappingProxyType({definition.name: definition for definition in definitions})


def _contains_control_payload(value: object) -> bool:
    if isinstance(value, str):
        return bool(_CONTROL_PAYLOAD.search(value))
    if isinstance(value, Mapping):
        return any(
            _contains_control_payload(key) or _contains_control_payload(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_control_payload(item) for item in value)
    return False


def _hashable_arguments(arguments: object) -> Mapping[str, Any]:
    if isinstance(arguments, Mapping):
        return {str(key): value for key, value in arguments.items()}
    return {"invalid_argument_type": type(arguments).__name__}


def _row_count(result: BaseModel) -> int:
    items = getattr(result, "items", None)
    return len(items) if isinstance(items, tuple) else 1


class OperationalToolExecutor:
    """The only authorized execution path for operational owner-chat tools."""

    def __init__(
        self,
        session: Session,
        profiles: ConnectionProfileRegistry,
        settings: Settings,
    ) -> None:
        self._session = session
        self._profiles = profiles
        self._settings = settings
        self.registry = build_operational_tool_registry(
            timeout_seconds=settings.operational_query_timeout_seconds
        )

    def available_definitions(
        self, user: User, business_id: uuid.UUID
    ) -> tuple[OperationalToolDefinition, ...]:
        definitions = list(self._primary_definitions(user, business_id))
        secret = self._settings.tool_call_audit_hmac_secret
        if secret is None or not secret.get_secret_value().strip():
            return ()
        from app.integrations.operational_quality import (
            FAMILY_TOOLS,
            evaluate_operational,
        )
        from app.services.capabilities import approved_adapter

        try:
            business = load_full_access_business(self._session, user, business_id)
            if business.status is not BusinessStatus.ACTIVE:
                return ()
        except ApplicationError:
            return ()
        operations = set()
        mapped_deadline = self._settings.operational_query_timeout_seconds
        for family, names in FAMILY_TOOLS.items():
            try:
                source = self._capability_source(business_id, family)
                if source is None:
                    continue
                adapter = approved_adapter(
                    self._session, self._profiles, source, names[0]
                )
                if not self._adapter_timeout_is_acceptable(adapter):
                    continue
                with adapter.catalogue.connector.connection(
                    consistent=True
                ) as connection:
                    quality = evaluate_operational(
                        adapter, connection, diagnostics=False, fingerprint=False
                    )
                if not quality.invalid:
                    mapped_deadline = max(
                        mapped_deadline, self._adapter_timeout_limit(adapter)
                    )
                    operations.update(
                        n for n in names if n in adapter.mapping.capabilities
                    )
            except SourceMappingError, ToolExecutionError:
                continue
        if operations:
            existing = next(
                (d for d in definitions if d.name == "operational_report"), None
            )
            if existing and existing.approved_operations is not None:
                operations.update(existing.approved_operations)
            if existing is None or existing.approved_operations is not None:
                definitions = [d for d in definitions if d.name != "operational_report"]
                definitions.append(
                    replace(
                        self.registry["operational_report"],
                        approved_operations=tuple(sorted(operations)),
                        timeout_seconds=mapped_deadline,
                    )
                )
        return tuple(definitions)

    def _primary_definitions(
        self, user: User, business_id: uuid.UUID
    ) -> tuple[OperationalToolDefinition, ...]:
        """Return tools only after a safe live source health preflight."""

        secret = self._settings.tool_call_audit_hmac_secret
        if secret is None or not secret.get_secret_value().strip():
            return ()

        try:
            business = load_full_access_business(self._session, user, business_id)
            if business.status is not BusinessStatus.ACTIVE:
                self._session.rollback()
                return ()
            source = self._active_source(business_id)
            if source is None:
                self._session.rollback()
                return ()
            capabilities = self._source_capabilities(source)
            if source.mapping_profile_key == "discovered_products":
                from app.services.operational_mapping import (
                    latest_operational_approval,
                    operational_source,
                )
                from app.services.source_mapping import mapped_source

                if source.status is not OperationalDataSourceStatus.ACTIVE:
                    return ()
                definitions = []
                try:
                    adapter = mapped_source(self._session, self._profiles, source)
                    if not self._adapter_timeout_is_acceptable(adapter):
                        return ()
                    adapter.validate_results()
                    definitions.append(
                        replace(
                            self.registry[PRODUCT_SEARCH_TOOL],
                            timeout_seconds=self._adapter_timeout_limit(adapter),
                        )
                    )
                except SourceMappingError:
                    pass
                if latest_operational_approval(self._session, source):
                    try:
                        operational = operational_source(
                            self._session, self._profiles, source
                        )
                        definitions.append(
                            replace(
                                self.registry["operational_report"],
                                approved_operations=operational.mapping.capabilities,
                            )
                        )
                    except SourceMappingError:
                        pass
                return tuple(definitions)
            adapter = self._profiles.resolve(source.connection_profile_key)
            if not self._adapter_timeout_is_acceptable(adapter):
                self._session.rollback()
                return ()
            self._session.commit()
            health = adapter.check_health()
            mapping = self._profiles.get_mapping(
                source.mapping_profile_key, source.mapping_profile_version
            )
            if mapping is None:
                return ()
            mapping.validate_health(health)
            return tuple(
                definition
                for definition in self.registry.values()
                if definition.capability in capabilities
            )
        except Exception as exc:
            _logger.warning(
                "available_definitions returning empty: %s",
                type(exc).__name__,
            )
            self._session.rollback()
            return ()

    def category_candidates(
        self, user: User, business_id: uuid.UUID
    ) -> tuple[CategoryCandidate, ...]:
        """Expose only bounded source categories to the operational planner."""
        secret = self._settings.tool_call_audit_hmac_secret
        if secret is None or not secret.get_secret_value().strip():
            return ()
        try:
            business = load_full_access_business(self._session, user, business_id)
            if business.status is not BusinessStatus.ACTIVE:
                self._session.rollback()
                return ()
            source = self._active_source(business_id)
            if source is None or "inventory" not in self._source_capabilities(source):
                self._session.rollback()
                return ()
            adapter = self._profiles.resolve(source.connection_profile_key)
            mapping = self._profiles.get_mapping(
                source.mapping_profile_key, source.mapping_profile_version
            )
            if mapping is None or not self._adapter_timeout_is_acceptable(adapter):
                self._session.rollback()
                return ()
            self._session.commit()
            health = adapter.check_health()
            mapping.validate_health(health)
            return adapter.list_categories(limit=MAX_CATEGORY_CANDIDATES)
        except Exception as exc:
            _logger.warning(
                "category_candidates returning empty: %s", type(exc).__name__
            )
            self._session.rollback()
            return ()

    def location_candidates(
        self, user: User, business_id: uuid.UUID
    ) -> tuple[LocationCandidate, ...]:
        """Expose only bounded, healthy inventory locations to the planner."""

        secret = self._settings.tool_call_audit_hmac_secret
        if secret is None or not secret.get_secret_value().strip():
            return ()
        try:
            business = load_full_access_business(self._session, user, business_id)
            if business.status is not BusinessStatus.ACTIVE:
                self._session.rollback()
                return ()
            source = self._active_source(business_id)
            if source is None or "inventory" not in self._source_capabilities(source):
                self._session.rollback()
                return ()
            adapter = self._profiles.resolve(source.connection_profile_key)
            mapping = self._profiles.get_mapping(
                source.mapping_profile_key, source.mapping_profile_version
            )
            if mapping is None or not self._adapter_timeout_is_acceptable(adapter):
                self._session.rollback()
                return ()
            self._session.commit()
            health = adapter.check_health()
            mapping.validate_health(health)
            return adapter.list_locations(limit=20)
        except Exception as exc:
            _logger.warning(
                "location_candidates returning empty: %s", type(exc).__name__
            )
            self._session.rollback()
            return ()

    def resolve_location(
        self, user: User, business_id: uuid.UUID, reference: str
    ) -> LocationResolution:
        secret = self._settings.tool_call_audit_hmac_secret
        if secret is None or not secret.get_secret_value().strip():
            raise ToolExecutionError("audit_unavailable")
        try:
            business = load_full_access_business(self._session, user, business_id)
            if business.status is not BusinessStatus.ACTIVE:
                raise ToolExecutionError("inactive_business")
            source = self._active_source(business_id)
            if source is None or "inventory" not in self._source_capabilities(source):
                raise ToolExecutionError("integration_unavailable")
            adapter = self._profiles.resolve(source.connection_profile_key)
            mapping = self._profiles.get_mapping(
                source.mapping_profile_key, source.mapping_profile_version
            )
            if mapping is None or not self._adapter_timeout_is_acceptable(adapter):
                raise ToolExecutionError("integration_unavailable")
            self._session.commit()
            health = adapter.check_health()
            mapping.validate_health(health)
            return adapter.resolve_location(
                LocationResolutionQuery(reference=reference)
            )
        except ToolExecutionError:
            self._session.rollback()
            raise
        except ApplicationError:
            self._session.rollback()
            raise ToolExecutionError("authorization_denied") from None
        except OperationalQueryTimeout:
            self._session.rollback()
            raise ToolExecutionError("timeout") from None
        except (
            MappingProfileError,
            OperationalDataInvalid,
            OperationalIntegrationError,
            OperationalSourceUnavailable,
            ValueError,
        ):
            self._session.rollback()
            raise ToolExecutionError("integration_unavailable") from None
        except Exception as exc:
            _logger.warning("resolve_location failed: %s", type(exc).__name__)
            self._session.rollback()
            raise ToolExecutionError("adapter_failure") from None

    def resolve_product(
        self, user: User, business_id: uuid.UUID, reference: str
    ) -> ProductResolution:
        """Resolve a bounded source product reference without recording a tool call."""

        secret = self._settings.tool_call_audit_hmac_secret
        if secret is None or not secret.get_secret_value().strip():
            raise ToolExecutionError("audit_unavailable")
        try:
            business = load_full_access_business(self._session, user, business_id)
            if business.status is not BusinessStatus.ACTIVE:
                raise ToolExecutionError("inactive_business")
            source = self._active_source(business_id)
            if source is None or "inventory" not in self._source_capabilities(source):
                raise ToolExecutionError("integration_unavailable")
            adapter = self._profiles.resolve(source.connection_profile_key)
            mapping = self._profiles.get_mapping(
                source.mapping_profile_key, source.mapping_profile_version
            )
            if mapping is None or not self._adapter_timeout_is_acceptable(adapter):
                raise ToolExecutionError("integration_unavailable")
            self._session.commit()
            health = adapter.check_health()
            mapping.validate_health(health)
            return adapter.resolve_product(ProductResolutionQuery(reference=reference))
        except ToolExecutionError:
            self._session.rollback()
            raise
        except ApplicationError:
            self._session.rollback()
            raise ToolExecutionError("authorization_denied") from None
        except OperationalQueryTimeout:
            self._session.rollback()
            raise ToolExecutionError("timeout") from None
        except (
            MappingProfileError,
            OperationalDataInvalid,
            OperationalIntegrationError,
            OperationalSourceUnavailable,
            ValueError,
        ):
            self._session.rollback()
            raise ToolExecutionError("integration_unavailable") from None
        except Exception as exc:
            _logger.warning("resolve_product failed: %s", type(exc).__name__)
            self._session.rollback()
            raise ToolExecutionError("adapter_failure") from None

    def execute(
        self,
        *,
        user: User,
        business_id: uuid.UUID,
        tool_name: object,
        arguments: object,
    ) -> OperationalToolResult:
        """Validate, authorize, execute, bound, and audit exactly one attempt."""

        started = time.monotonic()
        audit_name = (
            tool_name
            if isinstance(tool_name, str) and tool_name in self.registry
            else UNKNOWN_TOOL_AUDIT_NAME
        )
        secret = self._settings.tool_call_audit_hmac_secret
        if secret is None or not secret.get_secret_value().strip():
            raise ToolExecutionError("audit_unavailable")
        try:
            args_hash = hash_tool_arguments(_hashable_arguments(arguments), secret)
        except TypeError, ValueError:
            args_hash = hash_tool_arguments(
                {"invalid_arguments": type(arguments).__name__}, secret
            )

        result: BaseModel | None = None
        schema_fingerprint = None
        source_fingerprint = None
        source_id = None
        source_updated_at = None
        error_code: str | None = None
        failure_reason: str | None = None
        audit_status = ToolCallStatus.ERROR
        try:
            business = load_full_access_business(self._session, user, business_id)
            if business.status is not BusinessStatus.ACTIVE:
                raise ToolExecutionError("inactive_business")
            definition = (
                self.registry.get(tool_name) if isinstance(tool_name, str) else None
            )
            if definition is None:
                raise ToolExecutionError("unknown_tool")
            if not isinstance(arguments, Mapping) or _contains_control_payload(
                arguments
            ):
                raise ToolExecutionError("invalid_arguments")
            raw_limit = arguments.get("limit", 1)
            if (
                isinstance(raw_limit, int)
                and not isinstance(raw_limit, bool)
                and raw_limit > definition.result_limit
            ):
                raise ToolExecutionError("result_limit")
            try:
                encoded = json.dumps(arguments, ensure_ascii=False, allow_nan=False)
                query = definition.input_schema.model_validate_json(
                    encoded, strict=True
                )
            except TypeError, ValueError, ValidationError:
                raise ToolExecutionError("invalid_arguments") from None
            requested_limit = getattr(query, "limit", 1)
            if requested_limit > definition.result_limit:
                raise ToolExecutionError("result_limit")

            source = self._active_source(business_id)
            if source is None:
                raise ToolExecutionError("integration_unavailable")
            if definition.name == "operational_report":
                from app.services.capabilities import capability_family

                independent_source = self._capability_source(
                    business_id, capability_family(query.capability, query.rank_by)
                )
                if independent_source is not None:
                    source = independent_source
            capabilities = self._source_capabilities(source)
            if definition.capability not in capabilities:
                raise ToolExecutionError("capability_unavailable")
            if (
                definition.name == PRODUCT_SEARCH_TOOL
                and source.status is not OperationalDataSourceStatus.ACTIVE
            ):
                raise ToolExecutionError("capability_unavailable")
            source_id = source.id
            source_updated_at = source.updated_at
            is_catalogue = source.mapping_profile_key == "discovered_products"
            if is_catalogue and definition.name == "operational_report":
                from app.services.operational_mapping import operational_source

                adapter = operational_source(
                    self._session,
                    self._profiles,
                    source,
                    query.capability,
                    query.rank_by,
                )
                schema_fingerprint = adapter.catalogue.discovery.schema_fingerprint
                source_fingerprint = adapter.catalogue.discovery.source_fingerprint
            elif definition.name == PRODUCT_SEARCH_TOOL and is_catalogue:
                from app.services.source_mapping import mapped_source

                adapter = mapped_source(self._session, self._profiles, source)
                schema_fingerprint = adapter.discovery.schema_fingerprint
                source_fingerprint = adapter.discovery.source_fingerprint
            else:
                adapter = self._profiles.resolve(source.connection_profile_key)
            if definition.name == PRODUCT_SEARCH_TOOL:
                scope = self.catalogue_scope(source)
                schema_fingerprint = scope.schema_fingerprint
                source_fingerprint = scope.source_fingerprint
                if query.mapping_version is not None and (
                    query.mapping_version != scope.mapping_version
                ):
                    raise ToolExecutionError("integration_unavailable")
            if is_catalogue:
                definition = replace(
                    definition, timeout_seconds=self._adapter_timeout_limit(adapter)
                )
            if not self._adapter_timeout_is_acceptable(
                adapter, maximum_seconds=definition.timeout_seconds
            ):
                raise ToolExecutionError("integration_unavailable")
            if is_catalogue:
                self._session.commit()
                result = definition.executor(adapter, query)
            else:
                mapping = self._profiles.get_mapping(
                    source.mapping_profile_key, source.mapping_profile_version
                )
                if mapping is None:
                    raise ToolExecutionError("integration_unavailable")
                self._session.commit()
                health = adapter.check_health()
                mapping.validate_health(health)
                if (
                    definition.name == SALES_SUMMARY_TOOL
                    and getattr(query, "metric", "revenue")
                    not in mapping.supported_metrics
                ):
                    result = _unsupported_metric_result(
                        query,
                        mapping.supported_metrics,
                        health.source_timezone or "UTC",
                    )
                else:
                    result = definition.executor(adapter, query)
            source_result = result
            if definition.name == PRODUCT_SEARCH_TOOL and not is_catalogue:
                if not isinstance(result, ProductDiscoveryResult):
                    raise ToolExecutionError("adapter_failure")
                result = CatalogueResult(
                    mapping_version=scope.mapping_version,
                    continuation=result.continuation,
                    pagination_scope=result.pagination_scope,
                    discovery_request=result.discovery_request,
                    status=(
                        "not_found"
                        if not result.items
                        else "resolved"
                        if len(result.items) == 1 and not result.metadata.is_truncated
                        else "ambiguous"
                    ),
                    items=tuple(
                        CatalogueProduct(
                            external_product_id=item.external_product_id,
                            names=(item.name,),
                            sku=item.sku,
                            categories=(item.category,) if item.category else (),
                        )
                        for item in result.items
                    ),
                    truncated=result.metadata.is_truncated,
                )
            elapsed = time.monotonic() - started
            if elapsed > definition.timeout_seconds:
                raise ToolExecutionError("timeout")
            if not isinstance(result, definition.output_schema):
                raise ToolExecutionError("adapter_failure")
            if _row_count(result) > definition.result_limit:
                raise ToolExecutionError("result_limit")
            current = self._session.scalar(
                select(OperationalDataSourceConfig).where(
                    OperationalDataSourceConfig.id == source_id,
                    OperationalDataSourceConfig.business_id == business_id,
                )
            )
            if current is not None:
                self._session.refresh(current)
            if (
                current is None
                or (
                    current.status is not OperationalDataSourceStatus.ACTIVE
                    and not getattr(adapter, "independent", False)
                )
                or current.status is OperationalDataSourceStatus.DISABLED
                or (
                    current.updated_at != source_updated_at
                    and not getattr(adapter, "independent", False)
                )
            ):
                raise ToolExecutionError("integration_unavailable")
            if is_catalogue:
                from app.services.source_mapping import latest_approved

                revision = latest_approved(self._session, current)
                if not getattr(adapter, "independent", False) and (
                    revision is None
                    or revision.version
                    != (
                        adapter.mapping.catalogue_revision
                        if isinstance(adapter, MappedOperationalSource)
                        else adapter.revision_version
                    )
                    or revision.schema_fingerprint != schema_fingerprint
                ):
                    raise ToolExecutionError("integration_unavailable")
                if isinstance(adapter, MappedOperationalSource):
                    if getattr(adapter, "independent", False):
                        from app.services.capabilities import (
                            latest_event as capability_event,
                        )

                        current_event = capability_event(
                            self._session, current, adapter.capability_family
                        )
                        if (
                            current_event is None
                            or current_event.id != adapter.capability_approval_id
                            or current_event.approval_type == "revoked"
                            or current_event.expires_at <= datetime.now(UTC)
                        ):
                            raise ToolExecutionError("integration_unavailable")
                    else:
                        from app.services.operational_mapping import (
                            latest_operational_approval,
                        )

                        current_ops = latest_operational_approval(
                            self._session, current
                        )
                        if (
                            current_ops is None
                            or current_ops.version != adapter.revision
                        ):
                            raise ToolExecutionError("integration_unavailable")
            elif definition.name == PRODUCT_SEARCH_TOOL and (
                self.catalogue_scope(current) != scope
            ):
                raise ToolExecutionError("integration_unavailable")
            audit_status = ToolCallStatus.SUCCESS
        except ToolExecutionError as exc:
            error_code = exc.code
            audit_status = (
                ToolCallStatus.DENIED
                if exc.code
                in {
                    "unknown_tool",
                    "invalid_arguments",
                    "authorization_denied",
                    "inactive_business",
                    "integration_unavailable",
                    "capability_unavailable",
                }
                else ToolCallStatus.ERROR
            )
        except OperationalQueryTimeout:
            error_code = "timeout"
        except OperationalSourceUnavailable:
            error_code = "integration_unavailable"
        except OperationalDataInvalid, OperationalIntegrationError:
            error_code = "adapter_failure"
        except MappingProfileError:
            error_code = "integration_unavailable"
        except SourceMappingError as exc:
            if definition and definition.name == "operational_report":
                failure_reason = (
                    "resolution"
                    if exc.code == "catalogue_resolution_unapproved"
                    else "unapproved"
                    if exc.code.endswith("approval_required")
                    else "revoked"
                    if exc.code.endswith("approval_revoked")
                    else "reader"
                    if exc.code.startswith("source_")
                    else "validation"
                )
            error_code = (
                "invalid_arguments"
                if exc.code
                in {
                    "mapping_selection_stale",
                    "invalid_product_identifier",
                    "empty_product_search",
                    "invalid_catalogue_continuation",
                    "category_discovery_unsupported",
                }
                else "integration_unavailable"
            )
            audit_status = ToolCallStatus.DENIED
        except Exception as exc:
            error_code = (
                "authorization_denied"
                if isinstance(exc, ApplicationError)
                else "adapter_failure"
            )
            if error_code == "authorization_denied":
                audit_status = ToolCallStatus.DENIED

        latency_ms = max(0, int((time.monotonic() - started) * 1000))
        try:
            self._session.rollback()
            self._session.add(
                ToolCallLog(
                    business_id=business_id,
                    user_id=user.id,
                    tool_name=audit_name,
                    args_hash=args_hash,
                    status=audit_status,
                    error_code=error_code,
                    latency_ms=latency_ms,
                )
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise ToolExecutionError("audit_unavailable") from None

        if error_code is not None or result is None:
            raise ToolExecutionError(
                error_code or "adapter_failure", reason=failure_reason
            )
        return OperationalToolResult(
            tool_name=audit_name,
            output=result,
            latency_ms=latency_ms,
            source_id=source_id,
            source_updated_at=source_updated_at,
            schema_fingerprint=schema_fingerprint,
            source_fingerprint=source_fingerprint,
            source_result=source_result,
            mapping_version=getattr(
                result,
                "mapping_version",
                getattr(result, "mapping_revision", source.mapping_profile_version),
            ),
        )

    def reject(
        self,
        *,
        user: User,
        business_id: uuid.UUID,
        tool_name: object,
        arguments: object,
        code: str = "loop_limit",
    ) -> None:
        """Audit an execution request rejected before adapter invocation."""

        if code not in {"loop_limit", "invalid_arguments", "unknown_tool"}:
            code = "loop_limit"
        secret = self._settings.tool_call_audit_hmac_secret
        if secret is None or not secret.get_secret_value().strip():
            raise ToolExecutionError("audit_unavailable")
        try:
            args_hash = hash_tool_arguments(_hashable_arguments(arguments), secret)
        except TypeError, ValueError:
            args_hash = hash_tool_arguments(
                {"invalid_arguments": type(arguments).__name__}, secret
            )
        audit_name = (
            tool_name
            if isinstance(tool_name, str) and tool_name in self.registry
            else UNKNOWN_TOOL_AUDIT_NAME
        )
        try:
            load_full_access_business(self._session, user, business_id)
            self._session.add(
                ToolCallLog(
                    business_id=business_id,
                    user_id=user.id,
                    tool_name=audit_name,
                    args_hash=args_hash,
                    status=ToolCallStatus.DENIED,
                    error_code=code,
                    latency_ms=0,
                )
            )
            self._session.commit()
        except ApplicationError:
            self._session.rollback()
            raise ToolExecutionError("authorization_denied") from None
        except Exception:
            self._session.rollback()
            raise ToolExecutionError("audit_unavailable") from None
        raise ToolExecutionError(code)

    def _active_source(
        self, business_id: uuid.UUID
    ) -> OperationalDataSourceConfig | None:
        source = self._session.scalar(
            select(OperationalDataSourceConfig).where(
                OperationalDataSourceConfig.business_id == business_id,
                OperationalDataSourceConfig.status
                == OperationalDataSourceStatus.ACTIVE,
            )
        )
        if source is not None:
            return source
        from app.services.capabilities import active_capability_tools

        for candidate in self._managed_sources(business_id):
            if active_capability_tools(self._session, self._profiles, candidate):
                return candidate
        return None

    def _managed_sources(self, business_id: uuid.UUID):
        return self._session.scalars(
            select(OperationalDataSourceConfig)
            .where(
                OperationalDataSourceConfig.business_id == business_id,
                OperationalDataSourceConfig.mapping_profile_key
                == "discovered_products",
                OperationalDataSourceConfig.status
                != OperationalDataSourceStatus.DISABLED,
            )
            .order_by(
                OperationalDataSourceConfig.created_at, OperationalDataSourceConfig.id
            )
            .limit(10)
        ).all()

    def _capability_source(self, business_id: uuid.UUID, family: str):
        from app.services.capabilities import latest_event as capability_event

        candidates = []
        for source in self._managed_sources(business_id):
            event = capability_event(self._session, source, family)
            if (
                event
                and event.approval_type == "full"
                and event.expires_at > datetime.now(UTC)
            ):
                candidates.append(source)
        if len(candidates) > 1:
            selected = next(
                (
                    r
                    for r in candidates
                    if r.status is OperationalDataSourceStatus.ACTIVE
                ),
                None,
            )
            if selected is None:
                raise ToolExecutionError("capability_unavailable")
            return selected
        return candidates[0] if candidates else None

    def catalogue_scope(
        self, source: OperationalDataSourceConfig
    ) -> CatalogueSourceScope:
        """Versioned source authority shared by reads and conversation validation."""
        if "products" not in self._source_capabilities(source):
            raise ToolExecutionError("capability_unavailable")
        if source.mapping_profile_key == "discovered_products":
            from app.services.source_mapping import mapped_source

            adapter = mapped_source(self._session, self._profiles, source)
            if getattr(adapter, "quality_policy", None) is not None:
                # Cached conversation choices carry the same strict source boundary.
                adapter.validate_results()
            return CatalogueSourceScope(
                adapter.revision_version,
                adapter.discovery.schema_fingerprint,
                adapter.discovery.source_fingerprint,
            )
        # Fixed operational adapters have a versioned schema contract rather than
        # a discovered schema. The source stamp separately invalidates edits.
        mapping = self._profiles.get_mapping(
            source.mapping_profile_key, source.mapping_profile_version
        )
        schema = json.dumps(
            {
                "adapter": source.adapter_type,
                "mapping": mapping.key,
                "version": mapping.version,
                "capabilities": mapping.required_capabilities,
                "quantity": mapping.quantity_interpretation,
                "timezone": mapping.source_timezone,
                "currency": mapping.currency,
            },
            sort_keys=True,
        ).encode()
        identity = json.dumps(
            {
                "source": str(source.id),
                "business": str(source.business_id),
                "connection_profile": source.connection_profile_key,
            },
            sort_keys=True,
        ).encode()
        return CatalogueSourceScope(
            mapping.version,
            hashlib.sha256(schema).hexdigest(),
            hashlib.sha256(identity).hexdigest(),
        )

    def _source_capabilities(
        self, source: OperationalDataSourceConfig
    ) -> frozenset[str]:
        if source.mapping_profile_key == "discovered_products":
            from app.services.source_mapping import connector_for, latest_approved

            connector_for(self._profiles, source)
            from app.services.capabilities import active_capability_tools
            from app.services.operational_mapping import latest_operational_approval

            if not latest_approved(self._session, source):
                return (
                    frozenset({"mapped_operations"})
                    if active_capability_tools(self._session, self._profiles, source)
                    else frozenset()
                )
            return (
                frozenset({"products", "mapped_operations"})
                if latest_operational_approval(self._session, source)
                or active_capability_tools(self._session, self._profiles, source)
                else frozenset({"products"})
            )

        profile = self._profiles.get_profile(source.connection_profile_key)
        mapping = self._profiles.get_mapping(
            source.mapping_profile_key, source.mapping_profile_version
        )
        if (
            profile is None
            or mapping is None
            or (profile.business_ids and source.business_id not in profile.business_ids)
            or profile.adapter_type != source.adapter_type
            or profile.mapping_profile_key != source.mapping_profile_key
            or profile.mapping_profile_version != source.mapping_profile_version
        ):
            raise ToolExecutionError("integration_unavailable")
        mapping.validate_definition()
        return frozenset(mapping.required_capabilities)

    def _adapter_timeout_limit(self, adapter: OperationalDataSource) -> int:
        """Use only a typed, operator-managed connection's reviewed deadline."""
        connector = (
            getattr(getattr(adapter, "catalogue", None), "connector", None)
            if isinstance(adapter, MappedOperationalSource)
            else getattr(adapter, "connector", None)
            if isinstance(adapter, MappedProductSource)
            else None
        )
        config = getattr(connector, "config", None)
        if isinstance(config, SourceConnection):
            return config.query_timeout_seconds
        return self._settings.operational_query_timeout_seconds

    def _adapter_timeout_is_acceptable(
        self,
        adapter: OperationalDataSource,
        *,
        maximum_seconds: int | None = None,
    ) -> bool:
        try:
            enforced = adapter.enforced_query_timeout_seconds
        except Exception:
            return False
        maximum = maximum_seconds or self._adapter_timeout_limit(adapter)
        return (
            isinstance(enforced, int)
            and not isinstance(enforced, bool)
            and 1 <= enforced <= maximum
        )

    def sales_reporting_context(
        self, user: User, business_id: uuid.UUID
    ) -> tuple[str, tuple[str, ...]]:
        """Read allowlisted mapping semantics in the authorized source scope."""
        load_full_access_business(self._session, user, business_id)
        source = self._active_source(business_id)
        if source is None:
            raise ToolExecutionError("integration_unavailable")
        self._source_capabilities(source)
        if source.mapping_profile_key == "discovered_products":
            from app.services.operational_mapping import operational_source

            try:
                from app.services.capabilities import approved_adapter
                from app.services.capabilities import latest_event as capability_event

                if capability_event(self._session, source, "financial"):
                    mapping = approved_adapter(
                        self._session, self._profiles, source, "revenue"
                    ).mapping
                elif capability_event(self._session, source, "sales"):
                    mapping = approved_adapter(
                        self._session, self._profiles, source, "sales"
                    ).mapping
                else:
                    mapping = operational_source(
                        self._session, self._profiles, source
                    ).mapping
            except SourceMappingError as exc:
                raise ToolExecutionError("integration_unavailable") from exc
            metrics = tuple(
                name
                for name, supported in (
                    ("revenue", mapping.revenue),
                    ("sales_count", mapping.sales),
                )
                if supported
            )
            return mapping.timezone, metrics
        mapping = self._profiles.get_mapping(
            source.mapping_profile_key, source.mapping_profile_version
        )
        if mapping is None:
            raise ToolExecutionError("integration_unavailable")
        return mapping.source_timezone, mapping.supported_metrics
