"""Real PostgreSQL coverage for the external fake-store read-only adapter."""

from __future__ import annotations

import os
from collections.abc import Generator
from datetime import date
from decimal import Decimal

import pytest
from app.integrations.postgresql import PostgreSQLOperationalAdapter
from app.schemas.operational import (
    BestSellersQuery,
    InventoryQuery,
    RestockingQuery,
    SalesQuery,
)
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

FAKE_STORE_DATABASE_URL = os.getenv(
    "FAKE_STORE_DATABASE_URL",
    "postgresql+psycopg://sou2ai_store_reader:sou2ai_store_reader_local@"
    "127.0.0.1:5434/fake_store",
)


@pytest.fixture(scope="module")
def fake_store_engine() -> Generator[Engine]:
    engine = create_engine(
        FAKE_STORE_DATABASE_URL,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def operational_adapter() -> Generator[PostgreSQLOperationalAdapter]:
    adapter = PostgreSQLOperationalAdapter(
        FAKE_STORE_DATABASE_URL,
        connect_timeout_seconds=5,
        query_timeout_seconds=2,
    )
    yield adapter
    adapter.dispose()


def test_fake_store_health_is_safe_and_uses_configured_metadata(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    health = operational_adapter.check_health()

    assert health.status == "healthy"
    assert health.source_timezone == "Asia/Beirut"
    assert health.currency == "LBP"
    assert health.data_timestamp.isoformat() == "2026-08-24T06:00:00+00:00"
    assert "password" not in health.model_dump_json()


def test_product_inventory_normalization_and_valid_reservations(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    result = operational_adapter.get_current_inventory(
        InventoryQuery(branch_external_id="BR-BEY", product_filter="Rice", limit=10)
    )

    assert len(result.items) == 1
    item = result.items[0]
    assert item.product.external_product_id == "P1001"
    assert item.product.sku == "RICE-5KG"
    assert item.product.barcode == "5280001000011"
    assert item.product.category == "Pantry"
    assert item.branch_external_id == "BR-BEY"
    assert item.warehouse_external_id is None
    assert item.on_hand_quantity == Decimal("12")
    assert item.reserved_quantity == Decimal("3")
    assert item.available_quantity == Decimal("9")
    assert result.metadata.row_count == 1
    assert result.metadata.source_timezone == "Asia/Beirut"


def test_expired_reservations_are_not_subtracted(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    result = operational_adapter.get_current_inventory(
        InventoryQuery(
            branch_external_id="BR-BEY", product_filter="WATER-1500", limit=10
        )
    )

    assert result.items[0].reserved_quantity == Decimal("5")
    assert result.items[0].available_quantity == Decimal("35")


def test_inventory_branch_warehouse_isolation_and_literal_filtering(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    branch = operational_adapter.get_current_inventory(
        InventoryQuery(branch_external_id="BR-JBEIL", limit=100)
    )
    warehouse = operational_adapter.get_current_inventory(
        InventoryQuery(warehouse_external_id="WH-BEY", limit=100)
    )
    literal_wildcard = operational_adapter.get_current_inventory(
        InventoryQuery(product_filter="%", limit=100)
    )

    assert len(branch.items) == 6
    assert {item.branch_external_id for item in branch.items} == {"BR-JBEIL"}
    assert len(warehouse.items) == 6
    assert {item.warehouse_external_id for item in warehouse.items} == {"WH-BEY"}
    assert literal_wildcard.items == ()


def test_inventory_limit_is_enforced_and_reports_truncation(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    result = operational_adapter.get_current_inventory(InventoryQuery(limit=1))

    assert len(result.items) == 1
    assert result.metadata.requested_limit == 1
    assert result.metadata.row_count == 1
    assert result.metadata.is_truncated is True


def test_sales_totals_statuses_returns_currency_and_timezone(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    summary = operational_adapter.get_sales_summary(
        SalesQuery(start_date=date(2026, 8, 20), end_date=date(2026, 8, 23))
    )

    assert summary.completed_sale_count == 5
    assert summary.returned_sale_count == 1
    assert summary.completed_refund_count == 2
    assert summary.gross_quantity_sold == Decimal("28")
    assert summary.returned_quantity == Decimal("3")
    assert summary.net_quantity_sold == Decimal("25")
    assert summary.gross_revenue == Decimal("5440000")
    assert summary.refund_amount == Decimal("680000")
    assert summary.net_revenue == Decimal("4760000")
    assert summary.currency == "LBP"
    assert summary.period.source_timezone == "Asia/Beirut"


def test_reporting_uses_half_open_local_date_boundaries(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    before_boundary = operational_adapter.get_sales_summary(
        SalesQuery(start_date=date(2026, 8, 20), end_date=date(2026, 8, 22))
    )
    from_boundary = operational_adapter.get_sales_summary(
        SalesQuery(start_date=date(2026, 8, 22), end_date=date(2026, 8, 23))
    )

    assert before_boundary.completed_sale_count == 3
    assert before_boundary.returned_sale_count == 1
    assert before_boundary.completed_refund_count == 0
    assert before_boundary.gross_quantity_sold == Decimal("23")
    assert before_boundary.gross_revenue == Decimal("4380000")
    assert from_boundary.completed_sale_count == 2
    assert from_boundary.completed_refund_count == 2
    assert from_boundary.gross_quantity_sold == Decimal("5")
    assert from_boundary.returned_quantity == Decimal("3")
    assert from_boundary.net_revenue == Decimal("380000")


def test_best_seller_ranking_is_deterministic_and_refund_aware(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    result = operational_adapter.get_best_selling_products(
        BestSellersQuery(
            start_date=date(2026, 8, 20), end_date=date(2026, 8, 23), limit=10
        )
    )

    assert [item.product.external_product_id for item in result.items] == [
        "P1003",
        "P1002",
        "P1004",
        "P1001",
        "P1006",
    ]
    assert [item.rank for item in result.items] == [1, 2, 3, 4, 5]
    assert [item.quantity_sold for item in result.items] == [
        Decimal("9"),
        Decimal("5"),
        Decimal("5"),
        Decimal("3"),
        Decimal("3"),
    ]
    assert result.items[1].revenue == Decimal("900000")
    assert result.items[3].revenue == result.items[4].revenue == Decimal("1500000")


def test_best_seller_limit_and_branch_isolation(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    result = operational_adapter.get_best_selling_products(
        BestSellersQuery(
            start_date=date(2026, 8, 20),
            end_date=date(2026, 8, 23),
            branch_external_id="BR-JBEIL",
            limit=2,
        )
    )

    assert [item.product.external_product_id for item in result.items] == [
        "P1003",
        "P1006",
    ]
    assert result.metadata.is_truncated is True


def test_restocking_recommendations_use_available_stock_and_target(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    result = operational_adapter.get_restocking_recommendations(
        RestockingQuery(branch_external_id="BR-BEY", limit=100)
    )

    quantities = {
        item.inventory.product.external_product_id: item.recommended_quantity
        for item in result.items
    }
    assert quantities == {
        "P1001": Decimal("21"),
        "P1002": Decimal("12"),
        "P1004": Decimal("25"),
        "P1005": Decimal("11"),
    }


def test_readonly_role_has_only_required_source_access(
    fake_store_engine: Engine,
) -> None:
    with fake_store_engine.connect() as connection:
        identity = connection.execute(
            text(
                "SELECT current_database(), current_user, "
                "current_setting('transaction_read_only'), "
                "current_setting('statement_timeout')"
            )
        ).one()
        privileges = connection.execute(
            text(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
                "rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = current_user"
            )
        ).one()
        count = connection.scalar(text("SELECT count(*) FROM minimarket.catalog_items"))
        platform_table = connection.scalar(
            text("SELECT to_regclass('public.businesses')")
        )
        hidden = connection.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'private_internal'"
            )
        ).all()

    assert identity == ("fake_store", "sou2ai_store_reader", "on", "2s")
    assert privileges == (False, False, False, False, False)
    assert count == 6
    assert platform_table is None
    assert hidden == []


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO minimarket.categories (label) VALUES ('Forbidden')",
        "UPDATE minimarket.catalog_items SET display_label = 'Forbidden' "
        "WHERE item_id = 1",
        "DELETE FROM minimarket.stock_levels WHERE item_id = 1",
        "TRUNCATE minimarket.categories",
        "CREATE TABLE minimarket.forbidden (id integer)",
        "CREATE SCHEMA forbidden",
        "CREATE TEMP TABLE forbidden_temp (id integer)",
        "ALTER TABLE minimarket.catalog_items ADD COLUMN forbidden text",
        "SELECT * FROM private_internal.admin_notes",
    ],
)
def test_readonly_role_denies_mutation_creation_alteration_and_unrelated_reads(
    fake_store_engine: Engine, statement: str
) -> None:
    with fake_store_engine.connect() as connection:
        with pytest.raises(DBAPIError):
            connection.execute(text(statement))


def test_denied_operations_do_not_change_source_fixture(
    fake_store_engine: Engine,
) -> None:
    with fake_store_engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT count(*) FROM minimarket.categories")) == 5
        )
        assert (
            connection.scalar(text("SELECT count(*) FROM minimarket.catalog_items"))
            == 6
        )
