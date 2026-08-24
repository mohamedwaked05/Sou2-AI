"""Provider-neutral contracts for read-only operational business data."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

MAX_OPERATIONAL_ROWS = 100
MAX_BEST_SELLER_ROWS = 50
MAX_REPORTING_DAYS = 366


def _validate_source_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        raise ValueError("Source timezone must be a valid IANA timezone.") from None
    return value


class OperationalContract(BaseModel):
    """Strict immutable base for data crossing an operational adapter boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Product(OperationalContract):
    external_product_id: str = Field(min_length=1, max_length=128)
    sku: str | None = Field(default=None, min_length=1, max_length=128)
    barcode: str | None = Field(default=None, min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    category: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("external_product_id", "sku", "barcode", "name", "category")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Operational text fields cannot be blank.")
        return normalized


class InventoryItem(OperationalContract):
    product: Product
    branch_external_id: str | None = Field(default=None, min_length=1, max_length=128)
    branch_name: str | None = Field(default=None, min_length=1, max_length=255)
    warehouse_external_id: str | None = Field(
        default=None, min_length=1, max_length=128
    )
    warehouse_name: str | None = Field(default=None, min_length=1, max_length=255)
    on_hand_quantity: Decimal = Field(ge=0)
    reserved_quantity: Decimal = Field(ge=0)
    available_quantity: Decimal = Field(ge=0)
    reorder_point: Decimal = Field(ge=0)
    target_stock: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def validate_location_and_quantities(self) -> InventoryItem:
        branch = self.branch_external_id is not None or self.branch_name is not None
        warehouse = (
            self.warehouse_external_id is not None or self.warehouse_name is not None
        )
        if branch == warehouse:
            raise ValueError("Inventory must belong to one branch or one warehouse.")
        if (self.branch_external_id is None) != (self.branch_name is None):
            raise ValueError("Branch identifier and name must be supplied together.")
        if (self.warehouse_external_id is None) != (self.warehouse_name is None):
            raise ValueError("Warehouse identifier and name must be supplied together.")
        expected_available = max(
            self.on_hand_quantity - self.reserved_quantity, Decimal("0")
        )
        if self.available_quantity != expected_available:
            raise ValueError("Available quantity must subtract valid reservations.")
        if self.target_stock < self.reorder_point:
            raise ValueError("Target stock cannot be below the reorder point.")
        return self


class ReportingPeriod(OperationalContract):
    start_date: date
    end_date: date
    source_timezone: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_period(self) -> ReportingPeriod:
        days = (self.end_date - self.start_date).days
        if days < 1:
            raise ValueError("Reporting end date must be after the start date.")
        if days > MAX_REPORTING_DAYS:
            raise ValueError(
                f"Reporting periods cannot exceed {MAX_REPORTING_DAYS} days."
            )
        _validate_source_timezone(self.source_timezone)
        return self


class OperationalResultMetadata(OperationalContract):
    source_timezone: str = Field(min_length=1, max_length=64)
    data_timestamp: AwareDatetime
    queried_at: AwareDatetime
    freshness_seconds: int = Field(ge=0)
    row_count: int = Field(ge=0)
    requested_limit: int | None = Field(default=None, ge=1, le=MAX_OPERATIONAL_ROWS)
    is_truncated: bool = False

    @field_validator("source_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        return _validate_source_timezone(value)


class InventoryResult(OperationalContract):
    items: tuple[InventoryItem, ...]
    metadata: OperationalResultMetadata


class SalesSummary(OperationalContract):
    period: ReportingPeriod
    branch_external_id: str | None = Field(default=None, min_length=1, max_length=128)
    completed_sale_count: int = Field(ge=0)
    returned_sale_count: int = Field(ge=0)
    completed_refund_count: int = Field(ge=0)
    gross_quantity_sold: Decimal = Field(ge=0)
    returned_quantity: Decimal = Field(ge=0)
    net_quantity_sold: Decimal
    gross_revenue: Decimal = Field(ge=0)
    refund_amount: Decimal = Field(ge=0)
    net_revenue: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    metadata: OperationalResultMetadata

    @model_validator(mode="after")
    def validate_totals(self) -> SalesSummary:
        if self.net_quantity_sold != self.gross_quantity_sold - self.returned_quantity:
            raise ValueError("Net quantity must equal sold quantity minus returns.")
        if self.net_revenue != self.gross_revenue - self.refund_amount:
            raise ValueError("Net revenue must equal gross revenue minus refunds.")
        return self


class BestSellingProduct(OperationalContract):
    rank: int = Field(ge=1)
    product: Product
    quantity_sold: Decimal = Field(gt=0)
    revenue: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class BestSellingProductsResult(OperationalContract):
    period: ReportingPeriod
    branch_external_id: str | None = Field(default=None, min_length=1, max_length=128)
    items: tuple[BestSellingProduct, ...]
    metadata: OperationalResultMetadata


class RestockingRecommendation(OperationalContract):
    inventory: InventoryItem
    recommended_quantity: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def validate_recommendation(self) -> RestockingRecommendation:
        inventory = self.inventory
        expected = inventory.target_stock - inventory.available_quantity
        if inventory.available_quantity > inventory.reorder_point or expected <= 0:
            raise ValueError("Inventory does not meet the restocking rule.")
        if self.recommended_quantity != expected:
            raise ValueError("Recommendation must restore available stock to target.")
        return self


class RestockingRecommendationsResult(OperationalContract):
    items: tuple[RestockingRecommendation, ...]
    metadata: OperationalResultMetadata


class IntegrationHealth(OperationalContract):
    status: Literal["healthy", "unavailable"]
    checked_at: AwareDatetime
    source_timezone: str | None = Field(default=None, min_length=1, max_length=64)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    data_timestamp: AwareDatetime | None = None
    error_code: Literal["operational_source_unavailable"] | None = None

    @field_validator("source_timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        return _validate_source_timezone(value) if value is not None else None

    @model_validator(mode="after")
    def validate_health(self) -> IntegrationHealth:
        if self.status == "healthy":
            if (
                self.source_timezone is None
                or self.currency is None
                or self.data_timestamp is None
                or self.error_code is not None
            ):
                raise ValueError("Healthy integrations require safe source metadata.")
        elif self.error_code != "operational_source_unavailable":
            raise ValueError("Unavailable integrations require a safe error code.")
        return self


class InventoryQuery(OperationalContract):
    product_filter: str | None = Field(default=None, min_length=1, max_length=80)
    branch_external_id: str | None = Field(default=None, min_length=1, max_length=128)
    warehouse_external_id: str | None = Field(
        default=None, min_length=1, max_length=128
    )
    limit: int = Field(default=50, ge=1, le=MAX_OPERATIONAL_ROWS)

    @field_validator("product_filter", "branch_external_id", "warehouse_external_id")
    @classmethod
    def strip_filter(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Operational filters cannot be blank.")
        return normalized

    @model_validator(mode="after")
    def validate_location_filter(self) -> InventoryQuery:
        if self.branch_external_id and self.warehouse_external_id:
            raise ValueError("Filter by a branch or a warehouse, not both.")
        return self


class SalesQuery(OperationalContract):
    start_date: date
    end_date: date
    branch_external_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("branch_external_id")
    @classmethod
    def strip_branch_filter(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Operational filters cannot be blank.")
        return normalized

    @model_validator(mode="after")
    def validate_dates(self) -> SalesQuery:
        days = (self.end_date - self.start_date).days
        if days < 1:
            raise ValueError("Reporting end date must be after the start date.")
        if days > MAX_REPORTING_DAYS:
            raise ValueError(
                f"Reporting periods cannot exceed {MAX_REPORTING_DAYS} days."
            )
        return self

    def period(self, source_timezone: str) -> ReportingPeriod:
        return ReportingPeriod(
            start_date=self.start_date,
            end_date=self.end_date,
            source_timezone=source_timezone,
        )


class BestSellersQuery(SalesQuery):
    limit: int = Field(default=10, ge=1, le=MAX_BEST_SELLER_ROWS)


class RestockingQuery(InventoryQuery):
    pass
