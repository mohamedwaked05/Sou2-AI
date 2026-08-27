"""Provider-neutral registry and executor for approved operational tools."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
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
    MetricCapabilityResult,
    ProductResolution,
    ProductResolutionQuery,
    RestockingQuery,
    RestockingReadQuery,
    RestockingRecommendationsResult,
    SalesQuery,
    SalesSummary,
)
from app.services.businesses import load_full_access_business
from app.utils.argument_hashing import hash_tool_arguments

_logger = logging.getLogger(__name__)

CURRENT_INVENTORY_TOOL = "current_inventory"
SALES_SUMMARY_TOOL = "sales_summary"
BEST_SELLING_PRODUCTS_TOOL = "best_selling_products"
RESTOCKING_RECOMMENDATIONS_TOOL = "restocking_recommendations"
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


class BestSellingProductsToolInput(BestSellersQuery):
    limit: int = Field(default=10, ge=1, le=MAX_BEST_SELLER_RESULTS)


class RestockingRecommendationsToolInput(RestockingQuery):
    limit: int = Field(default=50, ge=1, le=MAX_TOOL_RESULT_ROWS)


class ToolExecutionError(Exception):
    """Safe, normalized failure from the centralized executor."""

    def __init__(self, code: str) -> None:
        if code not in SAFE_TOOL_ERROR_CODES:
            code = "adapter_failure"
        self.code = code
        super().__init__(code)


ToolExecutor = Callable[[OperationalDataSource, BaseModel], BaseModel]


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

    def provider_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema.model_json_schema(),
        }


@dataclass(frozen=True)
class OperationalToolResult:
    tool_name: str
    output: BaseModel
    latency_ms: int


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
        category_filter=query.category_filter,
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
            items=(), metadata=category_resolution.metadata, resolution=resolution
        )
    read_query = RestockingReadQuery(
        external_product_id=(
            resolution.product.external_product_id
            if resolution is not None and resolution.product is not None
            else None
        ),
        category_filter=query.category_filter,
        branch_external_id=query.branch_external_id,
        warehouse_external_id=query.warehouse_external_id,
        limit=query.limit,
    )
    result = source.get_restocking_recommendations(read_query)
    return result.model_copy(update={"resolution": resolution})


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
            name=CURRENT_INVENTORY_TOOL,
            description=(
                "Retrieve current product inventory, quantities, reservations, and "
                "availability for an optional exact ID, SKU, barcode, product name, "
                "approved alias, or source-resolved category and one branch or "
                "warehouse. Product resolution "
                "can explicitly be resolved, ambiguous, or not found."
            ),
            input_schema=CurrentInventoryToolInput,
            output_schema=InventoryResult,
            capability="inventory",
            result_limit=MAX_TOOL_RESULT_ROWS,
            timeout_seconds=timeout_seconds,
            executor=_inventory,
        ),
        OperationalToolDefinition(
            name=SALES_SUMMARY_TOOL,
            description=(
                "Summarize completed sales and finalized returns/refunds for a "
                "bounded source-local date range and optional branch. Revenue and "
                "sales count are supported only when selected by the typed metric; "
                "profit requires a separate mapped cost/expense capability."
            ),
            input_schema=SalesQuery,
            output_schema=(SalesSummary, MetricCapabilityResult),
            capability="sales_summaries",
            result_limit=1,
            timeout_seconds=timeout_seconds,
            executor=_sales_summary,
        ),
        OperationalToolDefinition(
            name=BEST_SELLING_PRODUCTS_TOOL,
            description=(
                "Rank best-selling products by net quantity for a bounded "
                "source-local date range and optional branch."
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
                "Calculate deterministic replenishment quantities from available "
                "stock, reorder points, and target stock for an optional exact ID, "
                "SKU, barcode, product name, approved alias, or source-resolved "
                "category. Product resolution "
                "can explicitly be resolved, ambiguous, or not found."
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
            adapter = self._profiles.resolve(source.connection_profile_key)
            if not self._adapter_timeout_is_acceptable(
                adapter, maximum_seconds=definition.timeout_seconds
            ):
                raise ToolExecutionError("integration_unavailable")
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
                and getattr(query, "metric", "revenue") not in mapping.supported_metrics
            ):
                result = _unsupported_metric_result(
                    query, mapping.supported_metrics, health.source_timezone or "UTC"
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
            if (
                current is None
                or current.status is not OperationalDataSourceStatus.ACTIVE
                or current.updated_at != source_updated_at
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
