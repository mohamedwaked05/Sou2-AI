"""Provider-neutral registry and executor for approved operational tools."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
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
from app.integrations.discovery import SourceMappingError
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
    ProductResolution,
    ProductResolutionQuery,
    RestockingQuery,
    RestockingReadQuery,
    RestockingRecommendationsResult,
    SalesQuery,
    SalesSummary,
)
from app.schemas.source_mapping import CatalogueRequest, CatalogueResult
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


class ProductSearchPlannerInput(BaseModel):
    """Search phrases only; approved selection scope is supplied by the backend."""

    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=20, ge=1, le=MAX_TOOL_RESULT_ROWS)

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Product searches cannot be blank.")
        return value


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

    def __init__(self, code: str) -> None:
        if code not in SAFE_TOOL_ERROR_CODES:
            code = "adapter_failure"
        self.code = code
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

    def provider_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": (
                self.provider_input_schema or self.input_schema
            ).model_json_schema(),
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


def _product_search(source: MappedProductSource, query: BaseModel) -> BaseModel:
    assert isinstance(source, MappedProductSource)
    assert isinstance(query, CatalogueRequest)
    return execute_product_search(source, query)


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
                from app.services.source_mapping import mapped_source

                adapter = mapped_source(self._session, self._profiles, source)
                if not self._adapter_timeout_is_acceptable(adapter):
                    self._session.rollback()
                    return ()
                expected_updated = source.updated_at
                self._session.commit()
                with adapter.connector.connection() as connection:
                    adapter.connector.assert_schema(adapter.discovery, connection)
                self._session.refresh(source)
                if (
                    source.status is not OperationalDataSourceStatus.ACTIVE
                    or source.updated_at != expected_updated
                ):
                    return ()
                return (self.registry[PRODUCT_SEARCH_TOOL],)
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
                and definition.name != PRODUCT_SEARCH_TOOL
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
            capabilities = self._source_capabilities(source)
            if definition.capability not in capabilities:
                raise ToolExecutionError("capability_unavailable")
            source_id = source.id
            source_updated_at = source.updated_at
            is_catalogue = source.mapping_profile_key == "discovered_products"
            if definition.name == PRODUCT_SEARCH_TOOL:
                if not is_catalogue:
                    raise ToolExecutionError("capability_unavailable")
                from app.services.source_mapping import mapped_source

                adapter = mapped_source(self._session, self._profiles, source)
                schema_fingerprint = adapter.discovery.schema_fingerprint
                source_fingerprint = adapter.discovery.source_fingerprint
            else:
                adapter = self._profiles.resolve(source.connection_profile_key)
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
                or current.status is not OperationalDataSourceStatus.ACTIVE
                or current.updated_at != source_updated_at
            ):
                raise ToolExecutionError("integration_unavailable")
            if is_catalogue:
                from app.services.source_mapping import latest_approved

                revision = latest_approved(self._session, current)
                if (
                    revision is None
                    or revision.version != adapter.revision_version
                    or revision.schema_fingerprint != schema_fingerprint
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
            error_code = (
                "invalid_arguments"
                if exc.code
                in {
                    "mapping_selection_stale",
                    "invalid_product_identifier",
                    "empty_product_search",
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
            raise ToolExecutionError(error_code or "adapter_failure")
        return OperationalToolResult(
            tool_name=audit_name,
            output=result,
            latency_ms=latency_ms,
            source_id=source_id,
            source_updated_at=source_updated_at,
            schema_fingerprint=schema_fingerprint,
            source_fingerprint=source_fingerprint,
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
        return self._session.scalar(
            select(OperationalDataSourceConfig).where(
                OperationalDataSourceConfig.business_id == business_id,
                OperationalDataSourceConfig.status
                == OperationalDataSourceStatus.ACTIVE,
            )
        )

    def _source_capabilities(
        self, source: OperationalDataSourceConfig
    ) -> frozenset[str]:
        if source.mapping_profile_key == "discovered_products":
            from app.services.source_mapping import connector_for, latest_approved

            connector_for(self._profiles, source)
            return (
                frozenset({"products"})
                if latest_approved(self._session, source)
                else frozenset()
            )
        profile = self._profiles.get_profile(source.connection_profile_key)
        mapping = self._profiles.get_mapping(
            source.mapping_profile_key, source.mapping_profile_version
        )
        if (
            profile is None
            or mapping is None
            or profile.adapter_type != source.adapter_type
            or profile.mapping_profile_key != source.mapping_profile_key
            or profile.mapping_profile_version != source.mapping_profile_version
        ):
            raise ToolExecutionError("integration_unavailable")
        mapping.validate_definition()
        return frozenset(mapping.required_capabilities)

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
        maximum = maximum_seconds or self._settings.operational_query_timeout_seconds
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
        mapping = self._profiles.get_mapping(
            source.mapping_profile_key, source.mapping_profile_version
        )
        if mapping is None:
            raise ToolExecutionError("integration_unavailable")
        return mapping.source_timezone, mapping.supported_metrics
