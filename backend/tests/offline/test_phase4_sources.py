"""Real parameterized query execution on disposable in-memory source databases."""

import socket
import sqlite3
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from app.integrations.discovery import SourceMappingError
from app.integrations.mapped_products import compile_catalogue
from app.integrations.operational import OperationalDataInvalid
from app.schemas.operational import (
    BestSellersQuery,
    InventoryReadQuery,
    RestockingReadQuery,
    SalesQuery,
)
from app.schemas.source_mapping import CatalogueRequest
from integration_sources import legacy_source, mapped_catalogue
from sqlalchemy import Engine, bindparam, column, select, table
from sqlalchemy.dialects import postgresql
from test_catalogue_chat import NOW


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    def deny(*_args, **_kwargs):
        raise AssertionError("Only literal SQLite :memory: fixtures are permitted")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(Engine, "connect", deny)


@pytest.fixture
def disposable_sources():
    sources = []
    yield sources
    for fixture in sources:
        fixture.source.close()


def test_expanded_parameters_remain_bound_across_sqlalchemy_versions(
    disposable_sources,
):
    fixture = mapped_catalogue(405)
    disposable_sources.append(fixture)
    fixture.source.readonly()
    hostile = "untrusted'); DROP TABLE records_405; --"
    products = table("records_405", column("identity"), schema="schema_405")
    statement = select(products.c.identity).where(
        products.c.identity.in_(bindparam("keys", expanding=True))
    )
    rows = fixture.source.execute(statement, {"keys": ["0001", hostile]})
    assert rows.fetchmany(10) == [{"identity": "0001"}]
    sql, parameters = fixture.source.calls[-1]
    assert hostile not in sql and hostile in parameters
    assert fixture.source.execute(statement, {"keys": []}).fetchmany(10) == []


@pytest.mark.parametrize("seed", [19, 73, 127, 991])
@pytest.mark.parametrize("layout", ["text", "integer", "composite"])
@pytest.mark.parametrize("engine", ["postgresql", "sqlserver"])
def test_alternative_catalogue_schemas_execute_search_and_exact_selection(
    disposable_sources, seed, layout, engine
):
    fixture = mapped_catalogue(seed, layout, engine)
    disposable_sources.append(fixture)
    adapter = fixture.adapter
    result = adapter.search(CatalogueRequest(query=f"صنف {seed}"))
    assert result.status == "ambiguous" and len(result.items) == 2
    assert result.items[0].names == result.items[1].names
    assert result.items[0].external_product_id != result.items[1].external_product_id
    assert all(p.categories == ("Group",) and p.stock is None for p in result.items)
    selected = adapter.search(
        CatalogueRequest(
            external_product_id=result.items[1].external_product_id, mapping_version=1
        )
    )
    assert selected.status == "resolved" and selected.items == (result.items[1],)
    limited = adapter.search(CatalogueRequest(query=fixture.family, limit=1))
    assert (
        limited.status == "ambiguous" and limited.truncated and len(limited.items) == 1
    )
    assert fixture.source.schema_checks == 6
    assert all("LIMIT" in sql for sql, _ in fixture.source.calls)
    assert adapter.validate_results()
    if layout != "integer":
        # Duplicate alternate identifier rows do not multiply product rows.
        alternate = adapter.search(CatalogueRequest(query="shared-code"))
        assert len(alternate.items) == 2


def test_source_databases_are_independent_and_readonly(disposable_sources):
    first, second = mapped_catalogue(55), mapped_catalogue(56)
    disposable_sources.extend([first, second])
    assert (
        second.adapter.search(CatalogueRequest(query=first.family)).status
        == "not_found"
    )
    assert (
        first.adapter.search(CatalogueRequest(query=second.family)).status
        == "not_found"
    )
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        first.source.db.execute("DELETE FROM schema_55.records_55")


@pytest.mark.parametrize(
    "term", ["%", "_", "[", "~", "' OR 1=1 --", "'; DROP TABLE records_81 --"]
)
def test_search_metacharacters_are_literal_and_queries_bounded(
    disposable_sources, term
):
    fixture = mapped_catalogue(81)
    disposable_sources.append(fixture)
    result = fixture.adapter.search(CatalogueRequest(query=term, limit=2))
    assert result.status == "not_found"
    sql, values = fixture.source.calls[-1]
    assert term in values and "LIMIT :" in sql
    if len(term) > 1:
        assert term not in sql
    assert fixture.adapter.search(CatalogueRequest(query=fixture.family)).items


def test_mapping_revision_and_actual_column_drift_fail_closed(disposable_sources):
    fixture = mapped_catalogue(92)
    disposable_sources.append(fixture)
    fixture.adapter.revision_version = 2
    with pytest.raises(SourceMappingError, match="mapping_selection_stale"):
        fixture.adapter.search(
            CatalogueRequest(external_product_id="0001", mapping_version=1)
        )
    assert not fixture.source.calls
    fixture.source.db.execute("PRAGMA query_only = OFF")
    fixture.source.db.execute(
        "ALTER TABLE schema_92.records_92 RENAME COLUMN label TO changed"
    )
    fixture.source.readonly()
    with pytest.raises(SourceMappingError, match="mapping_schema_changed"):
        fixture.adapter.search(CatalogueRequest(query=fixture.family))
    assert not fixture.source.calls


def test_drift_during_execution_discards_results(disposable_sources, monkeypatch):
    fixture = mapped_catalogue(94)
    disposable_sources.append(fixture)
    execute = fixture.source.execute

    def drift(statement, parameters):
        result = execute(statement, parameters)
        fixture.source.db.execute("PRAGMA query_only = OFF")
        fixture.source.db.execute(
            "ALTER TABLE schema_94.records_94 RENAME COLUMN label TO changed"
        )
        fixture.source.readonly()
        return result

    monkeypatch.setattr(fixture.source, "execute", drift)
    with pytest.raises(SourceMappingError, match="mapping_schema_changed"):
        fixture.adapter.search(CatalogueRequest(query=fixture.family))
    assert len(fixture.source.calls) == 1


def test_missing_source_labels_do_not_create_a_product(disposable_sources):
    fixture = mapped_catalogue(95)
    disposable_sources.append(fixture)
    fixture.source.db.execute("PRAGMA query_only = OFF")
    fixture.source.db.execute(
        "UPDATE schema_95.records_95 SET label=NULL, local_label=NULL "
        "WHERE identity='0001'"
    )
    fixture.source.readonly()
    with pytest.raises(SourceMappingError, match="catalogue_value_invalid"):
        fixture.adapter.search(
            CatalogueRequest(external_product_id="0001", mapping_version=1)
        )
    with pytest.raises(SourceMappingError, match="catalogue_value_invalid"):
        fixture.adapter._products([dict(key_0=None, name_0="Present", name_1=None)])


@pytest.mark.parametrize("engine", ["postgresql", "sqlserver"])
def test_native_dialects_compile_parameterized_bounded_queries(
    disposable_sources, engine
):
    if engine == "sqlserver":
        try:
            from sqlalchemy.dialects import mssql
        except OSError as exc:
            pytest.skip(f"Local SQL Server dialect files unavailable: {exc}")
        dialect = mssql.dialect()
    else:
        dialect = postgresql.dialect()
    fixture = mapped_catalogue(104, "composite")
    disposable_sources.append(fixture)
    statement, parameters = compile_catalogue(
        fixture.mapping,
        fixture.discovery,
        CatalogueRequest(query="private-search", limit=2),
    )
    compiled = statement.compile(dialect=dialect)
    assert "private-search" not in str(compiled)
    assert parameters["exact"] == "private-search"
    assert "records_104" in str(compiled)
    assert compiled.params["param_1"] == 3


@pytest.mark.parametrize(
    "currency,timezone",
    [
        ("EUR", "Asia/Beirut"),
        ("JPY", "Asia/Beirut"),
        ("USD", "Asia/Beirut"),
        ("EUR", "UTC"),
        ("EUR", "Europe/Paris"),
        ("JPY", "Asia/Tokyo"),
        ("USD", "America/New_York"),
    ],
)
def test_legacy_inventory_executes_reservations_locations_and_restocking(
    disposable_sources, currency, timezone
):
    try:
        ZoneInfo(timezone)
    except OSError as exc:
        pytest.skip(f"Local timezone files unavailable for {timezone}: {exc}")
    fixture = legacy_source(611, NOW, currency, timezone)
    disposable_sources.append(fixture)
    fixture.source.readonly()
    result = fixture.adapter.get_current_inventory(InventoryReadQuery())
    assert len(result.items) == 2
    first, second = result.items
    assert first.branch_external_id == "L1" and second.warehouse_external_id == "L2"
    assert first.on_hand_quantity == Decimal("10.5")
    assert first.reserved_quantity == Decimal(
        "3"
    ) and first.available_quantity == Decimal("7.5")
    assert second.available_quantity == 4
    assert result.metadata.source_timezone == timezone
    filtered = fixture.adapter.get_current_inventory(
        InventoryReadQuery(warehouse_external_id="L2")
    )
    assert filtered.items == (second,)
    restock = fixture.adapter.get_restocking_recommendations(RestockingReadQuery())
    assert len(restock.items) == 1 and restock.items[0].recommended_quantity == Decimal(
        "12.5"
    )
    bounded = fixture.adapter.get_current_inventory(InventoryReadQuery(limit=1))
    assert len(bounded.items) == 1 and bounded.metadata.is_truncated
    assert fixture.adapter.check_health().currency == currency


@pytest.mark.parametrize(
    "currency,timezone",
    [
        ("EUR", "Asia/Beirut"),
        ("JPY", "Asia/Beirut"),
        ("USD", "Asia/Beirut"),
        ("EUR", "UTC"),
        ("EUR", "Europe/Paris"),
        ("JPY", "Asia/Tokyo"),
        ("USD", "America/New_York"),
    ],
)
def test_legacy_sales_execute_period_refunds_and_configured_location_types(
    disposable_sources, currency, timezone
):
    try:
        ZoneInfo(timezone)
    except OSError as exc:
        pytest.skip(f"Local timezone files unavailable for {timezone}: {exc}")
    fixture = legacy_source(713, NOW, currency, timezone)
    disposable_sources.append(fixture)
    query = SalesQuery(
        start_date="2026-03-29", end_date="2026-03-30", branch_external_id="L1"
    )
    _, start, end = fixture.adapter._reporting_bounds(
        query, {"business_timezone": timezone}
    )
    rows = [
        (1, "COMPLETED", start, 1, 2.5, 100.5),
        (2, "RETURNED", end - timedelta(seconds=1), 1, 1.5, 30),
        (3, "PENDING", start, 1, 1000, 10000),
        (4, "CANCELLED", start, 1, 1000, 10000),
        (5, "COMPLETED", start - timedelta(days=1), 1, 7, 70),
        (6, "COMPLETED", start, 2, 4, 400),
        (7, "COMPLETED", end, 1, 900, 9000),
    ]
    for identifier, state, moment, location, quantity, total in rows:
        fixture.source.db.execute(
            "INSERT INTO minimarket.receipts VALUES (?,?,?,?)",
            (identifier, state, moment.isoformat(" "), location),
        )
        fixture.source.db.execute(
            "INSERT INTO minimarket.receipt_lines VALUES (?,?,1,?,?)",
            (identifier, identifier, quantity, total),
        )
    for identifier, receipt, state, moment, quantity, total in [
        (1, 5, "COMPLETED", start, 0.5, 10),
        (2, 1, "PENDING", start, 50, 500),
        (3, 1, "COMPLETED", end, 50, 500),
        (4, 1, "COMPLETED", start, 0.5, 5),
    ]:
        fixture.source.db.execute(
            "INSERT INTO minimarket.refunds VALUES (?,?,?,?)",
            (identifier, receipt, state, moment.isoformat(" ")),
        )
        fixture.source.db.execute(
            "INSERT INTO minimarket.refund_lines VALUES (?,?,?,?)",
            (identifier, receipt, quantity, total),
        )
    fixture.source.readonly()
    result = fixture.adapter.get_sales_summary(query)
    assert result.currency == currency and result.period.source_timezone == timezone
    assert (
        result.completed_sale_count,
        result.returned_sale_count,
        result.completed_refund_count,
    ) == (1, 1, 2)
    assert result.net_quantity_sold == 3 and result.net_revenue == Decimal("115.5")
    best = fixture.adapter.get_best_selling_products(
        BestSellersQuery(**query.model_dump(exclude={"metric"}))
    )
    assert (
        len(best.items) == 1 and best.items[0].quantity_sold == result.net_quantity_sold
    )
    assert (
        best.items[0].revenue == result.net_revenue
        and best.items[0].currency == currency
    )
    if timezone == "Europe/Paris":
        assert (end - start).total_seconds() == 23 * 3600


def test_invalid_source_quantities_are_rejected_without_fabricated_stock(
    disposable_sources,
):
    fixture = legacy_source(801, NOW, "EUR", "Asia/Beirut")
    disposable_sources.append(fixture)
    fixture.source.db.execute(
        "UPDATE minimarket.stock_levels SET physical_quantity=-2 WHERE location_id=1"
    )
    fixture.source.readonly()
    with pytest.raises(OperationalDataInvalid):
        fixture.adapter.get_current_inventory(InventoryReadQuery())
