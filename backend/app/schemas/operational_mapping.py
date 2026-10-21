"""Reviewed operational semantics. Identifiers reference discovered metadata only.

These contracts deliberately cannot carry SQL expressions. Amounts are source
amounts at an explicitly approved grain, never prices inferred from receipts.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from app.schemas.source_mapping import MappingContract, SchemaDiscovery

CurrencyCodes = dict[
    Annotated[str, Field(min_length=1, max_length=32)],
    Annotated[str, Field(pattern=r"^[A-Z]{3}$")],
]


class FieldRef(MappingContract):
    object_id: str = Field(pattern=r"^o[0-9]{1,3}$")
    column: str = Field(min_length=1, max_length=128)


class FactJoin(MappingContract):
    """A fact-to-dimension join must target an enforced unique key."""

    target_object_id: str = Field(pattern=r"^o[0-9]{1,3}$")
    fact_columns: tuple[str, ...] = Field(min_length=1, max_length=8)
    target_columns: tuple[str, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def matching_columns(self):
        if len(self.fact_columns) != len(self.target_columns):
            raise ValueError("Join arity differs.")
        return self


class RowScope(MappingContract):
    """Mandatory partition, approved for this exact source and business."""

    field: FieldRef
    value: str | int


class SourceFacts(MappingContract):
    object_id: str = Field(pattern=r"^o[0-9]{1,3}$")
    key_columns: tuple[str, ...] = Field(min_length=1, max_length=8)
    product_columns: tuple[str, ...] = Field(default=(), max_length=4)
    product_reference: Literal["catalogue_identity", "fact_identity", "none"] = (
        "catalogue_identity"
    )
    joins: tuple[FactJoin, ...] = Field(default=(), max_length=4)
    row_scopes: tuple[RowScope, ...] = Field(default=(), max_length=8)
    location: FieldRef | None = None
    branch: FieldRef | None = None
    warehouse: FieldRef | None = None
    unit: FieldRef | None = None


class StockMapping(SourceFacts):
    capability: Literal["stock"] = "stock"
    basis: Literal["authoritative_balance"] = "authoritative_balance"
    on_hand: FieldRef
    reserved: FieldRef | None = None
    available: FieldRef | None = None
    reservations_complete: bool = False
    availability_rule: Literal[
        "source", "subtract_complete_reservations", "unsupported"
    ]
    negative_policy: Literal["preserve", "clamp_available_zero"] = "preserve"
    observed_at: FieldRef | None = None
    observed_timezone: Literal["utc", "source_local"] | None = None
    reorder_point: FieldRef | None = None
    target_quantity: FieldRef | None = None
    reorder_trigger: Literal["below", "at_or_below"] | None = None
    restock_basis: Literal["available", "on_hand"] | None = None

    @model_validator(mode="after")
    def coherent_availability(self):
        if (
            not self.product_columns
            or self.unit is None
            or self.product_reference == "none"
        ):
            raise ValueError("Balances require a product identity and quantity unit.")
        if self.observed_at is not None and self.observed_timezone is None:
            raise ValueError("Freshness timestamps need an explicit timezone basis.")
        if self.availability_rule == "source" and self.available is None:
            raise ValueError("Authoritative availability needs a source field.")
        if self.availability_rule == "subtract_complete_reservations" and (
            self.reserved is None
            or not self.reservations_complete
            or self.available is not None
        ):
            raise ValueError(
                "Availability subtraction needs complete, non-net reservations."
            )
        if self.reorder_point is not None and (
            self.reorder_trigger is None or self.restock_basis is None
        ):
            raise ValueError("Restocking requires an approved trigger and stock basis.")
        if (
            self.restock_basis == "available"
            and self.availability_rule == "unsupported"
        ):
            raise ValueError("Restocking cannot use unsupported availability.")
        if bool(self.reorder_point) != bool(self.target_quantity):
            raise ValueError("Restocking requires both threshold and target.")
        return self


class PriceMapping(SourceFacts):
    capability: Literal["prices"] = "prices"
    amount: FieldRef
    currency: FieldRef
    currency_codes: CurrencyCodes = Field(
        default_factory=dict, max_length=16, exclude_if=lambda value: not value
    )
    price_list: FieldRef | None = None
    valid_from: FieldRef | None = None
    valid_until: FieldRef | None = None
    tax_basis: Literal["included", "excluded", "unknown"]
    precedence: Literal["require_unique", "latest_effective"] = "require_unique"
    customer_scope: Literal["general"] = "general"

    @model_validator(mode="after")
    def explicit_precedence(self):
        if (
            not self.product_columns
            or self.unit is None
            or self.product_reference == "none"
        ):
            raise ValueError("Prices require a product identity and selling unit.")
        if self.precedence == "latest_effective" and self.valid_from is None:
            raise ValueError("Effective-date precedence needs an effective-date field.")
        return self


class SalesMapping(SourceFacts):
    capability: Literal["sales"] = "sales"
    transaction_columns: tuple[FieldRef, ...] = Field(min_length=1, max_length=8)
    transaction_identity: Literal[
        "enforced_header_key", "reviewed_denormalized_key"
    ] = "enforced_header_key"
    grain: Literal["product_events", "transaction_events"] = "product_events"
    quantity: FieldRef | None = None
    occurred_at: FieldRef
    timestamp_basis: Literal["utc", "source_local", "business_date"]
    status: FieldRef
    completed_statuses: tuple[str, ...] = Field(min_length=1, max_length=12)
    excluded_statuses: tuple[str, ...] = Field(default=(), max_length=24)
    # Values must be nonnegative. Returns are a separate, completed event grain.
    quantity_semantics: Literal["gross_nonnegative"] = "gross_nonnegative"
    quantity_multiplier: Literal[-1, 1] = 1
    category: FieldRef | None = None
    returns_semantics: Literal["separate_complete", "no_returns", "unsupported"] = (
        "unsupported"
    )

    @model_validator(mode="after")
    def coherent_grain(self):
        if self.grain == "product_events" and (
            not self.product_columns
            or self.unit is None
            or self.quantity is None
            or self.product_reference == "none"
        ):
            raise ValueError("Product sales require identity, units and quantity.")
        if self.grain == "transaction_events" and (
            self.product_columns
            or self.product_reference != "none"
            or self.quantity is not None
            or self.unit is not None
        ):
            raise ValueError(
                "Transaction totals carry no invented product quantities or units."
            )
        if self.grain == "transaction_events" and (
            self.transaction_identity != "enforced_header_key"
            or any(ref.object_id != self.object_id for ref in self.transaction_columns)
            or set(ref.column for ref in self.transaction_columns)
            != set(self.key_columns)
        ):
            raise ValueError(
                "Transaction totals require the complete enforced fact key "
                "as their transaction identity."
            )
        return self


class RevenueMapping(MappingContract):
    capability: Literal["revenue"] = "revenue"
    amount_multiplier: Literal[-1, 1] = 1
    gross_amount: FieldRef
    net_amount: FieldRef | None = None
    net_rule: Literal["source_net", "gross_minus_discount"] = "source_net"
    discount_amount: FieldRef | None = None
    currency: FieldRef
    currency_codes: CurrencyCodes = Field(
        default_factory=dict, max_length=16, exclude_if=lambda value: not value
    )
    net_basis: Literal["before_refunds", "already_net_of_refunds"]
    tax_basis: Literal["included", "excluded"]
    discount_basis: Literal["net_includes_discounts"] = "net_includes_discounts"
    recognition_basis: Literal["sales_event"] = "sales_event"
    rounding: Literal["source_line_amounts"] = "source_line_amounts"

    @model_validator(mode="after")
    def explicit_net_rule(self):
        if (
            self.net_rule == "source_net"
            and (self.net_amount is None or self.discount_amount is not None)
        ) or (
            self.net_rule == "gross_minus_discount"
            and (self.discount_amount is None or self.net_amount is not None)
        ):
            raise ValueError(
                "Net revenue requires one explicit source-net or "
                "gross-minus-discount definition."
            )
        return self


class RefundMapping(SalesMapping):
    amount_multiplier: Literal[-1, 1] = 1
    tax_basis: Literal["included", "excluded"] | None = None
    capability: Literal["refunds"] = "refunds"
    original_transaction_columns: tuple[FieldRef, ...] = Field(
        min_length=1, max_length=8
    )
    amount: FieldRef | None = None
    currency: FieldRef | None = None
    currency_codes: CurrencyCodes = Field(
        default_factory=dict, max_length=16, exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def money_pair(self):
        if self.currency_codes and self.currency is None:
            raise ValueError("Currency translations require a currency source field.")
        if self.amount is not None and self.tax_basis is None:
            raise ValueError("Refund amounts need an explicit tax basis.")
        if bool(self.amount) != bool(self.currency):
            raise ValueError("Refund amounts need an explicit currency.")
        return self


class OperationalMapping(MappingContract):
    contract_version: Literal[1] = 1
    business_id: uuid.UUID
    source_id: uuid.UUID
    catalogue_revision: int = Field(ge=0)
    source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    schema_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    timezone: str
    external_scope: Literal["dedicated", "partitioned"]
    # A reviewer must attest every joined object is in the same business scope.
    scope_reviewed: Literal[True]
    stock: StockMapping | None = None
    prices: PriceMapping | None = None
    sales: SalesMapping | None = None
    revenue: RevenueMapping | None = None
    refunds: RefundMapping | None = None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("Unknown source timezone.") from None
        return value

    @model_validator(mode="after")
    def dependencies(self):
        if not any((self.stock, self.prices, self.sales)):
            raise ValueError("An operational mapping needs at least one capability.")
        if self.revenue and not self.sales:
            raise ValueError("Revenue needs the approved sales event grain.")
        if self.refunds and not self.sales:
            raise ValueError("Refunds need an approved sales definition.")
        if self.refunds and self.refunds.grain != self.sales.grain:
            raise ValueError(
                "Refunds must use the same quantity/transaction grain as sales."
            )
        if (
            self.revenue
            and self.refunds
            and self.revenue.tax_basis != self.refunds.tax_basis
        ):
            raise ValueError("Sales and refund amounts require the same tax basis.")
        if self.revenue and self.refunds and self.refunds.amount is None:
            raise ValueError(
                "Net financial reporting needs authoritative refund amounts."
            )
        if self.refunds and self.sales.returns_semantics != "separate_complete":
            raise ValueError(
                "Refund subtraction requires a complete separate returns ledger."
            )
        if (
            self.sales
            and self.sales.returns_semantics == "separate_complete"
            and not self.refunds
        ):
            raise ValueError("A complete separate returns ledger must be mapped.")
        if (
            self.revenue
            and self.revenue.net_basis == "before_refunds"
            and not self.refunds
            and self.sales.returns_semantics != "no_returns"
        ):
            raise ValueError(
                "Net revenue needs complete refunds or an approved no-returns policy."
            )
        if self.external_scope == "partitioned":
            for fact in (self.stock, self.prices, self.sales, self.refunds):
                if fact and not fact.row_scopes:
                    raise ValueError(
                        "Shared sources need mandatory fact row partitions."
                    )
        return self

    @property
    def capabilities(self) -> tuple[str, ...]:
        values = [
            name
            for name in ("stock", "prices", "sales", "revenue", "refunds")
            if getattr(self, name)
        ]
        if self.stock and self.stock.availability_rule != "unsupported":
            values.append("availability")
        if self.stock and self.stock.reorder_point:
            values.append("restocking")
        if self.sales and self.sales.grain == "product_events":
            values.append("best_sellers")
        return tuple(values)


class OperationalRead(MappingContract):
    capability: Literal[
        "stock",
        "availability",
        "prices",
        "sales",
        "revenue",
        "refunds",
        "best_sellers",
        "restocking",
    ]
    product_id: str | None = Field(default=None, min_length=1, max_length=4096)
    catalogue_revision: int = Field(ge=0)
    mapping_revision: int = Field(ge=1)
    location: str | None = Field(default=None, min_length=1, max_length=255)
    branch: str | None = Field(default=None, min_length=1, max_length=255)
    warehouse: str | None = Field(default=None, min_length=1, max_length=255)
    category: str | None = Field(default=None, min_length=1, max_length=255)
    price_list: str | None = Field(default=None, min_length=1, max_length=255)
    start: datetime | None = None
    end: datetime | None = None
    as_of: datetime | None = None
    rank_by: Literal["net_quantity", "net_revenue"] = "net_quantity"
    limit: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="after")
    def bounded_period(self):
        if self.capability in {"sales", "revenue", "refunds", "best_sellers"}:
            if (
                not self.start
                or not self.end
                or not self.start.tzinfo
                or not self.end.tzinfo
            ):
                raise ValueError("Reporting requires explicit aware bounds.")
            if not 0 < (self.end - self.start).total_seconds() <= 366 * 86400:
                raise ValueError("Invalid or excessive reporting period.")
        if self.as_of and not self.as_of.tzinfo:
            raise ValueError("Pricing requires an aware instant.")
        if self.capability == "prices" and (
            self.product_id is None or self.as_of is None
        ):
            raise ValueError("Pricing requires an exact product and effective instant.")
        return self


class OperationalRow(MappingContract):
    product_id: str | None = None
    location: str | None = None
    branch: str | None = None
    warehouse: str | None = None
    unit: str | None = None
    currency: str | None = None
    on_hand: Decimal | None = None
    reserved: Decimal | None = None
    available: Decimal | None = None
    price: Decimal | None = None
    price_list: str | None = None
    tax_basis: str | None = None
    gross_quantity: Decimal | None = None
    returned_quantity: Decimal | None = None
    net_quantity: Decimal | None = None
    transaction_count: int | None = None
    gross_revenue: Decimal | None = None
    net_revenue: Decimal | None = None
    refunds: Decimal | None = None
    reorder_point: Decimal | None = None
    target_quantity: Decimal | None = None
    recommended_quantity: Decimal | None = None
    observed_at: datetime | None = None


class OperationalTotal(MappingContract):
    """Quantity totals are unit-scoped; money totals are currency-scoped."""

    unit: str | None = None
    currency: str | None = None
    gross_quantity: Decimal | None = None
    returned_quantity: Decimal | None = None
    net_quantity: Decimal | None = None
    gross_revenue: Decimal | None = None
    net_revenue: Decimal | None = None
    refunds: Decimal | None = None
    transaction_count: int


class OperationalReport(MappingContract):
    business_meaning: str | None = Field(default=None, max_length=2000)
    start: datetime | None = None
    end: datetime | None = None
    as_of: datetime | None = None
    revenue_basis: str | None = None
    capability: str
    status: Literal["ok", "not_found", "ambiguous", "unsupported"]
    reason: str | None = None
    rows: tuple[OperationalRow, ...] = Field(default=(), max_length=50)
    totals: tuple[OperationalTotal, ...] = Field(default=(), max_length=50)
    truncated: bool = False
    business_id: uuid.UUID
    source_id: uuid.UUID
    mapping_revision: int
    catalogue_revision: int
    source_fingerprint: str
    schema_fingerprint: str
    timezone: str


class OperationalMappingApproval(MappingContract):
    mapping: OperationalMapping
    discovery: SchemaDiscovery
    confirm_semantics: Literal[True]
    confirm_row_isolation: Literal[True]


class OperationalMappingDraft(MappingContract):
    mapping: OperationalMapping
    discovery: SchemaDiscovery


class OperationalMappingPreview(OperationalMappingDraft):
    request: OperationalRead


class OperationalPlannerRead(MappingContract):
    """Language interpretation only; the backend supplies all authority/version IDs."""

    capability: Literal[
        "stock",
        "availability",
        "prices",
        "sales",
        "revenue",
        "refunds",
        "best_sellers",
        "restocking",
    ]
    product_query: str | None = Field(default=None, min_length=1, max_length=128)
    product_identity: tuple[str, ...] | None = Field(
        default=None, min_length=1, max_length=4
    )
    reference: Literal["current", "selected"] = "current"
    location: str | None = Field(default=None, min_length=1, max_length=255)
    branch: str | None = Field(default=None, min_length=1, max_length=255)
    warehouse: str | None = Field(default=None, min_length=1, max_length=255)
    category: str | None = Field(default=None, min_length=1, max_length=255)
    price_list: str | None = Field(default=None, min_length=1, max_length=255)
    start: datetime | None = None
    end: datetime | None = None
    rank_by: Literal["net_quantity", "net_revenue"] = "net_quantity"
    limit: int = Field(default=20, ge=1, le=50)

    @field_validator("product_identity")
    @classmethod
    def bounded_identity(cls, value):
        if value is not None and any(
            not part.strip() or len(part) > 128 for part in value
        ):
            raise ValueError("Provide original bounded identity components only.")
        return value


class OperationalToolRead(OperationalPlannerRead):
    product_id: str | None = Field(default=None, min_length=1, max_length=4096)
    catalogue_revision: int | None = Field(default=None, ge=1)
