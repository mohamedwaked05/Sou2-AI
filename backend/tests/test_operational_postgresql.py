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
    InventoryReadQuery,
    LocationResolutionQuery,
    ProductResolutionQuery,
    RestockingReadQuery,
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
    assert operational_adapter.enforced_query_timeout_seconds == 2


@pytest.mark.parametrize(
    ("reference", "matched_by"),
    [
        ("3", "internal_id"),
        ("P1003", "external_id"),
        ("WATER-1500", "sku"),
        ("5280001000035", "barcode"),
        ("Nestle Pure Life Water 1.5 L", "name"),
        ("مياه نستله", "alias"),
        ("مي نستله", "alias"),
        ("mayyet Nestle", "alias"),
    ],
)
def test_product_resolution_exact_identifiers_names_and_aliases(
    operational_adapter: PostgreSQLOperationalAdapter,
    reference: str,
    matched_by: str,
) -> None:
    resolution = operational_adapter.resolve_product(
        ProductResolutionQuery(reference=reference)
    )

    assert resolution.status == "resolved"
    assert resolution.matched_by == matched_by
    assert resolution.product is not None
    assert resolution.product.external_product_id == "P1003"
    assert resolution.candidates == ()


def test_exact_resolution_precedes_partial_and_partial_codes_match_source_fields(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    exact = operational_adapter.resolve_product(
        ProductResolutionQuery(reference="Pepsi Can 330 ml")
    )
    partial_code = operational_adapter.resolve_product(
        ProductResolutionQuery(reference="P10")
    )

    assert exact.status == "resolved"
    assert exact.matched_by == "name"
    assert exact.product is not None
    assert exact.product.external_product_id == "P1007"
    assert partial_code.status == "ambiguous"
    assert partial_code.matched_by == "partial_name"
    assert partial_code.candidates


def test_partial_product_name_is_ambiguous_with_bounded_safe_candidates(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    resolution = operational_adapter.resolve_product(
        ProductResolutionQuery(reference="Pepsi", candidate_limit=2)
    )

    assert resolution.status == "ambiguous"
    assert resolution.matched_by == "partial_name"
    assert [candidate.external_product_id for candidate in resolution.candidates] == [
        "P1008",
        "P1007",
    ]
    assert all(
        set(candidate.model_dump(exclude_none=True))
        <= {"external_product_id", "sku", "barcode", "name"}
        for candidate in resolution.candidates
    )


def test_natural_language_product_reference_is_resolved_by_source_tokens(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    resolution = operational_adapter.resolve_product(
        ProductResolutionQuery(reference="how many pepsi we have left")
    )

    assert resolution.status == "ambiguous"
    assert {candidate.name for candidate in resolution.candidates} == {
        "Pepsi Can 330 ml",
        "Pepsi Bottle 1.5 L",
    }


def test_unknown_product_is_explicitly_not_found(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    resolution = operational_adapter.resolve_product(
        ProductResolutionQuery(reference="Does Not Exist")
    )

    assert resolution.status == "not_found"
    assert resolution.product is None
    assert resolution.candidates == ()


def test_one_resolved_product_preserves_its_separate_inventory_locations(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    resolution = operational_adapter.resolve_product(
        ProductResolutionQuery(reference="WATER-1500")
    )
    assert resolution.product is not None

    inventory = operational_adapter.get_current_inventory(
        InventoryReadQuery(
            external_product_id=resolution.product.external_product_id, limit=10
        )
    )

    assert len(inventory.items) == 3
    assert {item.product.external_product_id for item in inventory.items} == {"P1003"}
    assert {
        item.branch_external_id or item.warehouse_external_id
        for item in inventory.items
    } == {"BR-BEY", "BR-JBEIL", "WH-BEY"}


def test_product_inventory_normalization_and_valid_reservations(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    result = operational_adapter.get_current_inventory(
        InventoryReadQuery(
            branch_external_id="BR-BEY", external_product_id="P1001", limit=10
        )
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
        InventoryReadQuery(
            branch_external_id="BR-BEY", external_product_id="P1003", limit=10
        )
    )

    assert result.items[0].reserved_quantity == Decimal("5")
    assert result.items[0].available_quantity == Decimal("35")


def test_inventory_branch_warehouse_isolation_and_literal_filtering(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    branch = operational_adapter.get_current_inventory(
        InventoryReadQuery(branch_external_id="BR-JBEIL", limit=100)
    )
    warehouse = operational_adapter.get_current_inventory(
        InventoryReadQuery(warehouse_external_id="WH-BEY", limit=100)
    )

    assert len(branch.items) == 8
    assert {item.branch_external_id for item in branch.items} == {"BR-JBEIL"}
    assert len(warehouse.items) == 8
    assert {item.warehouse_external_id for item in warehouse.items} == {"WH-BEY"}


def test_inventory_stable_branch_id_matches_uppercase_source_type(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    result = operational_adapter.get_current_inventory(
        InventoryReadQuery(
            external_product_id="P1004", branch_external_id="BR-JBEIL", limit=5
        )
    )

    assert len(result.items) == 1
    assert result.items[0].branch_external_id == "BR-JBEIL"
    assert result.items[0].warehouse_external_id is None
    assert result.items[0].available_quantity == Decimal("50")


def test_saved_and_explicit_branch_references_return_identical_inventory(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    """A persisted canonical preference compiles like an explicit location filter."""

    saved_preference_filter = InventoryReadQuery(
        external_product_id="P1004", branch_external_id="BR-JBEIL", limit=5
    )
    explicit_filter = InventoryReadQuery(
        external_product_id="P1004", branch_external_id="BR-JBEIL", limit=5
    )

    saved = operational_adapter.get_current_inventory(saved_preference_filter)
    explicit = operational_adapter.get_current_inventory(explicit_filter)

    assert saved.metadata.row_count == explicit.metadata.row_count == 1
    assert [item.model_dump() for item in saved.items] == [
        item.model_dump() for item in explicit.items
    ]
    assert saved.items[0].available_quantity == Decimal("50")


def test_inventory_limit_is_enforced_and_reports_truncation(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    result = operational_adapter.get_current_inventory(InventoryReadQuery(limit=1))

    assert len(result.items) == 1
    assert result.metadata.requested_limit == 1
    assert result.metadata.row_count == 1
    assert result.metadata.is_truncated is True


def test_category_resolution_and_inventory_filter_use_source_labels(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    resolution = operational_adapter.resolve_category(
        ProductResolutionQuery(reference="Beverages")
    )

    assert resolution.status == "resolved"
    assert resolution.category is not None
    assert resolution.category.label == "Beverages"

    result = operational_adapter.get_current_inventory(
        InventoryReadQuery(category_filter=resolution.category.label, limit=100)
    )

    assert result.items
    assert {item.product.category for item in result.items} == {"Beverages"}


def test_category_candidates_are_bounded_and_source_defined(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    candidates = operational_adapter.list_categories(limit=2)

    assert len(candidates) == 2
    assert all(candidate.external_category_id for candidate in candidates)
    assert [candidate.label for candidate in candidates] == sorted(
        candidate.label for candidate in candidates
    )


def test_category_resolution_reports_ambiguous_source_matches(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    resolution = operational_adapter.resolve_category(
        ProductResolutionQuery(reference="a")
    )

    assert resolution.status == "ambiguous"
    assert len(resolution.candidates) >= 2


def test_category_resolution_reports_not_found_for_an_unknown_source_concept(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    resolution = operational_adapter.resolve_category(
        ProductResolutionQuery(reference="No matching catalogue category")
    )

    assert resolution.status == "not_found"
    assert resolution.category is None
    assert resolution.candidates == ()
    assert resolution.metadata.row_count == 0


def test_location_resolution_uses_bounded_source_labels_and_codes(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    by_label = operational_adapter.resolve_location(
        LocationResolutionQuery(reference="jbeil")
    )
    by_code = operational_adapter.resolve_location(
        LocationResolutionQuery(reference="BR-JBEIL")
    )

    assert by_label.status == by_code.status == "resolved"
    assert by_label.location is not None
    assert by_code.location is not None
    assert (
        by_label.location.external_location_id == by_code.location.external_location_id
    )
    assert by_label.location.label == by_code.location.label
    assert by_label.location.location_type == "branch"


def test_location_resolution_is_case_insensitive_and_literal(
    operational_adapter: PostgreSQLOperationalAdapter,
) -> None:
    resolution = operational_adapter.resolve_location(
        LocationResolutionQuery(reference="JB%_EIL")
    )

    assert resolution.status == "not_found"
    assert resolution.candidates == ()


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
        RestockingReadQuery(branch_external_id="BR-BEY", limit=100)
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
        alias_count = connection.scalar(
            text("SELECT count(*) FROM minimarket.catalog_item_aliases")
        )
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
    assert count == 8
    assert alias_count == 8
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
        "UPDATE minimarket.catalog_item_aliases SET approved = false",
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
            == 8
        )
