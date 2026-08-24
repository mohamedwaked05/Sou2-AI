"""Unit coverage for provider-neutral operational contracts and safe failures."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from app.core.config import Settings
from app.integrations.operational import (
    OperationalDataSource,
    OperationalQueryTimeout,
    OperationalSourceUnavailable,
)
from app.integrations.postgresql import PostgreSQLOperationalAdapter
from app.schemas.operational import (
    BestSellersQuery,
    IntegrationHealth,
    InventoryItem,
    InventoryQuery,
    InventoryReadQuery,
    OperationalResultMetadata,
    Product,
    ProductResolution,
    ProductResolutionCandidate,
    ProductResolutionQuery,
    ReportingPeriod,
    RestockingRecommendation,
    SalesQuery,
    SalesSummary,
)
from pydantic import SecretStr, ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError


def product() -> Product:
    return Product(
        external_product_id=" P1001 ",
        sku=" RICE-5KG ",
        barcode="5280001000011",
        name=" Cedars Long Grain Rice 5 kg ",
        category=" Pantry ",
    )


def metadata() -> OperationalResultMetadata:
    queried_at = datetime(2026, 8, 24, 12, tzinfo=UTC)
    return OperationalResultMetadata(
        source_timezone="Asia/Beirut",
        data_timestamp=queried_at - timedelta(minutes=5),
        queried_at=queried_at,
        freshness_seconds=300,
        row_count=1,
    )


def inventory() -> InventoryItem:
    return InventoryItem(
        product=product(),
        branch_external_id="BR-BEY",
        branch_name="Achrafieh Branch",
        on_hand_quantity=Decimal("12"),
        reserved_quantity=Decimal("3"),
        available_quantity=Decimal("9"),
        reorder_point=Decimal("10"),
        target_stock=Decimal("30"),
    )


def test_product_normalizes_external_identifiers_without_source_column_names() -> None:
    normalized = product()

    assert normalized.external_product_id == "P1001"
    assert normalized.sku == "RICE-5KG"
    assert normalized.name == "Cedars Long Grain Rice 5 kg"
    assert "item_code" not in Product.model_fields


def test_contracts_reject_blank_product_text_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Product(
            external_product_id=" ",
            name="Rice",
            source_table="catalog_items",  # type: ignore[call-arg]
        )


def test_product_resolution_contract_enforces_explicit_safe_states() -> None:
    resolved = ProductResolution(
        status="resolved",
        matched_by="sku",
        product=product(),
        metadata=metadata(),
    )
    ambiguous = ProductResolution(
        status="ambiguous",
        matched_by="partial_name",
        candidates=(
            ProductResolutionCandidate(
                external_product_id="P1007", sku="PEPSI-330", name="Pepsi 330 ml"
            ),
            ProductResolutionCandidate(
                external_product_id="P1008",
                sku="PEPSI-1500",
                name="Pepsi 1.5 L",
            ),
        ),
        metadata=metadata(),
    )
    not_found = ProductResolution(status="not_found", metadata=metadata())

    assert resolved.product == product()
    assert len(ambiguous.candidates) == 2
    assert not_found.product is None
    with pytest.raises(ValidationError, match="exactly one"):
        ProductResolution(status="resolved", matched_by="sku", metadata=metadata())
    with pytest.raises(ValidationError, match="at least two"):
        ProductResolution(
            status="ambiguous",
            matched_by="partial_name",
            candidates=(ambiguous.candidates[0],),
            metadata=metadata(),
        )


def test_product_resolution_query_is_strict_and_bounded() -> None:
    assert ProductResolutionQuery(reference="  Pepsi  ").reference == "Pepsi"
    with pytest.raises(ValidationError):
        ProductResolutionQuery(reference="Pepsi", candidate_limit=6)
    with pytest.raises(ValidationError):
        ProductResolutionQuery.model_validate(
            {"reference": "Pepsi", "business_id": "not-allowed"}
        )


def test_inventory_requires_one_location_and_correct_available_quantity() -> None:
    assert inventory().available_quantity == Decimal("9")

    with pytest.raises(ValidationError, match="valid reservations"):
        InventoryItem.model_validate(
            {**inventory().model_dump(), "available_quantity": Decimal("12")}
        )

    values = inventory().model_dump()
    values["warehouse_external_id"] = "WH-BEY"
    values["warehouse_name"] = "Beirut Central Warehouse"
    with pytest.raises(ValidationError, match="one branch or one warehouse"):
        InventoryItem.model_validate(values)


def test_sales_summary_enforces_net_quantity_and_revenue_semantics() -> None:
    period = ReportingPeriod(
        start_date=date(2026, 8, 20),
        end_date=date(2026, 8, 23),
        source_timezone="Asia/Beirut",
    )
    summary = SalesSummary(
        period=period,
        completed_sale_count=5,
        returned_sale_count=1,
        completed_refund_count=2,
        gross_quantity_sold=Decimal("28"),
        returned_quantity=Decimal("3"),
        net_quantity_sold=Decimal("25"),
        gross_revenue=Decimal("5440000"),
        refund_amount=Decimal("680000"),
        net_revenue=Decimal("4760000"),
        currency="LBP",
        metadata=metadata(),
    )

    assert summary.net_revenue == Decimal("4760000")
    with pytest.raises(ValidationError, match="Net revenue"):
        SalesSummary.model_validate(
            {**summary.model_dump(), "net_revenue": Decimal("1")}
        )


def test_restocking_is_deterministic_from_structured_inventory() -> None:
    recommendation = RestockingRecommendation(
        inventory=inventory(), recommended_quantity=Decimal("21")
    )
    assert recommendation.recommended_quantity == Decimal("21")

    with pytest.raises(ValidationError, match="restore"):
        RestockingRecommendation(
            inventory=inventory(), recommended_quantity=Decimal("20")
        )


@pytest.mark.parametrize(
    "query",
    [
        lambda: SalesQuery(start_date=date(2026, 8, 20), end_date=date(2026, 8, 20)),
        lambda: SalesQuery(start_date=date(2025, 1, 1), end_date=date(2026, 2, 1)),
        lambda: InventoryQuery(limit=101),
        lambda: BestSellersQuery(
            start_date=date(2026, 8, 20), end_date=date(2026, 8, 23), limit=51
        ),
        lambda: InventoryQuery(
            branch_external_id="BR-BEY", warehouse_external_id="WH-BEY"
        ),
        lambda: InventoryQuery(product_filter=" "),
    ],
)
def test_queries_reject_invalid_ranges_filters_and_oversized_requests(
    query: Any,
) -> None:
    with pytest.raises(ValidationError):
        query()


def test_reporting_period_requires_a_real_iana_timezone() -> None:
    with pytest.raises(ValidationError, match="IANA"):
        ReportingPeriod(
            start_date=date(2026, 8, 20),
            end_date=date(2026, 8, 21),
            source_timezone="Lebanon/SecretStore",
        )


def test_health_contract_exposes_only_safe_metadata() -> None:
    health = IntegrationHealth(
        status="unavailable",
        checked_at=datetime.now(UTC),
        error_code="operational_source_unavailable",
    )
    assert health.model_dump(exclude_none=True) == {
        "status": "unavailable",
        "checked_at": health.checked_at,
        "error_code": "operational_source_unavailable",
    }


class TimeoutCause(Exception):
    sqlstate = "57014"


class TimeoutEngine:
    def connect(self) -> Any:
        raise OperationalError(
            "SELECT secret",
            {},
            TimeoutCause("postgresql://reader:password@secret-host:5434/fake_store"),
        )

    def dispose(self) -> None:
        return None


class UnavailableEngine:
    def connect(self) -> Any:
        raise OperationalError(
            "SELECT secret",
            {},
            ConnectionError("postgresql://reader:password@secret-host:5434/fake_store"),
        )

    def dispose(self) -> None:
        return None


def test_query_timeout_and_health_errors_do_not_leak_credentials(caplog: Any) -> None:
    adapter = PostgreSQLOperationalAdapter(
        engine=cast(Engine, TimeoutEngine()), query_timeout_seconds=1
    )

    with pytest.raises(OperationalQueryTimeout) as raised:
        adapter.get_current_inventory(InventoryReadQuery())

    assert str(raised.value) == "Operational source query timed out."
    health = adapter.check_health()
    combined = f"{raised.value} {health.model_dump_json()} {caplog.text}"
    assert "password" not in combined
    assert "secret-host" not in combined
    assert "postgresql://" not in combined


def test_non_timeout_database_errors_are_safely_normalized(caplog: Any) -> None:
    adapter = PostgreSQLOperationalAdapter(
        engine=cast(Engine, UnavailableEngine()), query_timeout_seconds=1
    )

    with pytest.raises(OperationalSourceUnavailable) as raised:
        adapter.get_sales_summary(
            SalesQuery(start_date=date(2026, 8, 20), end_date=date(2026, 8, 21))
        )

    combined = f"{raised.value} {caplog.text}"
    assert str(raised.value) == "Operational source is unavailable."
    assert "password" not in combined
    assert "secret-host" not in combined


def test_settings_keep_the_operational_connection_url_secret() -> None:
    settings = Settings(
        _env_file=None,
        fake_store_database_url=(
            "postgresql+psycopg://reader:local-password@127.0.0.1:5434/fake_store"
        ),
    )

    assert isinstance(settings.fake_store_database_url, SecretStr)
    assert "local-password" not in repr(settings.fake_store_database_url)


def test_postgresql_adapter_satisfies_the_provider_neutral_protocol() -> None:
    adapter = PostgreSQLOperationalAdapter(engine=cast(Engine, TimeoutEngine()))
    assert isinstance(adapter, OperationalDataSource)
    assert not hasattr(adapter, "execute_sql")
    assert not hasattr(adapter, "inspect_schema")
