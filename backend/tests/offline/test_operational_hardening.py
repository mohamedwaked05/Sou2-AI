"""Fail-closed semantics at the real compiler/calculation boundary."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.integrations.discovery import SourceMappingError
from app.integrations.mapped_operational import validate_operational_mapping
from app.schemas.operational_mapping import OperationalMapping, OperationalRead
from pydantic import ValidationError
from test_mapped_operations import NOW, read
from test_mapped_operations import source as source


def test_unmanaged_adapter_cannot_broaden_the_tool_deadline(source):
    from app.core.config import Settings
    from app.tools.operational import OperationalToolExecutor

    executor = OperationalToolExecutor(
        None,
        None,
        Settings(
            _env_file=None, environment="testing", operational_query_timeout_seconds=2
        ),
    )
    source.catalogue.connector.config.query_timeout_seconds = 5
    assert not executor._adapter_timeout_is_acceptable(source.adapter)


def test_empty_operational_approval_cannot_advertise_support(source):
    payload = source.mapping.model_dump()
    for capability in ("stock", "prices", "sales", "revenue", "refunds"):
        payload[capability] = None
    with pytest.raises(ValidationError, match="at least one capability"):
        OperationalMapping.model_validate(payload)


@pytest.mark.parametrize("name", ["on_hand", "reserved", "unit", "observed_at"])
def test_wrong_column_types_rejected_before_execution(source, name):
    wrong = "balance" if name in {"unit", "observed_at"} else "unit"
    payload = source.mapping.model_dump()
    payload["stock"][name] = dict(object_id="o1", column=source.tables[1].c[wrong].name)
    if name == "observed_at":
        payload["stock"]["observed_timezone"] = "utc"
    with pytest.raises(SourceMappingError, match="mapping_.*type"):
        validate_operational_mapping(
            OperationalMapping.model_validate(payload),
            source.discovery,
            source.catalogue.mapping,
        )


def test_large_report_fails_instead_of_returning_partial_totals(source, monkeypatch):
    import app.integrations.mapped_operational as compiler

    monkeypatch.setattr(compiler, "MAX_FACTS", 0)
    with pytest.raises(SourceMappingError, match="operational_fact_budget_exceeded"):
        read(source, "sales")


def test_missing_product_row_cannot_silently_drop_stock(source):
    with source.engine.begin() as connection:
        connection.execute(
            source.tables[0].delete().where(source.tables[0].c.tenant == "A")
        )
    with pytest.raises(SourceMappingError, match="operational_product_invalid"):
        read(source, "stock")


def test_mixed_units_cannot_be_ranked(source):
    with source.engine.begin() as connection:
        connection.execute(
            source.tables[0].insert(), dict(tenant="A", code="002", label="Another")
        )
        connection.execute(
            source.tables[3].insert(),
            dict(
                id=4,
                tenant="A",
                product="002",
                location="west",
                unit="piece",
                transaction="ticket2",
                quantity=10,
                at=NOW,
                state="completed",
                category="approved-class",
                gross=15,
                net=12,
                currency="USD",
            ),
        )
    assert len(read(source, "sales").rows) == 2
    with pytest.raises(
        SourceMappingError, match="ranking_incompatible_units_or_currencies"
    ):
        read(source, "best_sellers")


@pytest.mark.parametrize("rank_by", ["net_quantity", "net_revenue"])
def test_mixed_currencies_are_not_merged_or_ranked(source, rank_by):
    with source.engine.begin() as connection:
        connection.execute(
            source.tables[3].insert(),
            dict(
                id=4,
                tenant="A",
                product="001",
                location="west",
                unit="kg",
                transaction="ticket2",
                quantity=10,
                at=NOW,
                state="completed",
                category="approved-class",
                gross=15,
                net=12,
                currency="EUR",
            ),
        )
    assert {row.currency for row in read(source, "revenue").rows} == {"USD", "EUR"}
    with pytest.raises(
        SourceMappingError, match="ranking_incompatible_units_or_currencies"
    ):
        read(source, "best_sellers", rank_by=rank_by)


@pytest.mark.parametrize("value", ["", " " * 3])
def test_missing_units_are_not_fabricated(source, value):
    with source.engine.begin() as connection:
        connection.execute(
            source.tables[1]
            .update()
            .where(source.tables[1].c.tenant == "A")
            .values(unit=value)
        )
    with pytest.raises(SourceMappingError, match="operational_text_invalid"):
        read(source, "stock")


def test_source_available_is_not_subtracted_twice(source):
    payload = source.mapping.model_dump()
    payload["stock"].update(
        available=payload["stock"]["on_hand"], availability_rule="source"
    )
    source.adapter.mapping = OperationalMapping.model_validate(payload)
    row = read(source, "availability").rows[0]
    assert row.available == row.on_hand == Decimal("-2.1250")


def test_restock_thresholds_are_optional_not_fabricated(source):
    source.adapter.mapping = source.mapping.model_copy(
        update={
            "stock": source.mapping.stock.model_copy(
                update={"reorder_point": None, "target_quantity": None}
            )
        }
    )
    assert read(source, "stock").rows[0].reorder_point is None
    assert read(source, "restocking").status == "unsupported"


def test_returns_and_availability_require_explicit_evidence(source):
    payload = source.mapping.model_dump()
    payload["stock"]["reservations_complete"] = False
    with pytest.raises(ValidationError):
        OperationalMapping.model_validate(payload)
    payload = source.mapping.model_dump()
    payload["refunds"] = None
    with pytest.raises(ValidationError):
        OperationalMapping.model_validate(payload)


def test_join_target_must_be_unique(source):
    payload = source.mapping.model_dump()
    payload["stock"]["joins"] = [
        dict(
            target_object_id="o2", fact_columns=["product"], target_columns=["product"]
        )
    ]
    with pytest.raises(SourceMappingError, match="mapping_join_not_unique"):
        validate_operational_mapping(
            OperationalMapping.model_validate(payload),
            source.discovery,
            source.catalogue.mapping,
        )


def test_half_open_reporting_boundaries(source):
    query = OperationalRead(
        capability="sales",
        catalogue_revision=1,
        mapping_revision=1,
        start=NOW,
        end=NOW + timedelta(seconds=1),
    )
    assert source.adapter.read(query).rows[0].gross_quantity == 2
    assert (
        source.adapter.read(
            query.model_copy(update={"start": NOW - timedelta(seconds=1), "end": NOW})
        ).status
        == "not_found"
    )


def test_local_dst_transition_fails_explicitly(source):
    obj = source.discovery.objects[3]
    columns = tuple(
        c.model_copy(update={"type_signature": "timestamp without time zone"})
        if c.name == source.mapping.sales.occurred_at.column
        else c
        for c in obj.columns
    )
    objects = (
        *source.discovery.objects[:3],
        obj.model_copy(update={"columns": columns}),
        *source.discovery.objects[4:],
    )
    source.discovery = source.discovery.model_copy(update={"objects": objects})
    source.catalogue.discovery = source.discovery
    source.mapping = source.mapping.model_copy(
        update={"schema_fingerprint": source.discovery.schema_fingerprint}
    )
    sales = source.mapping.sales.model_copy(update={"timestamp_basis": "source_local"})
    source.adapter.mapping = source.mapping.model_copy(update={"sales": sales})
    with pytest.raises(
        SourceMappingError, match="local_timestamp_dst_interval_unsupported"
    ):
        source.adapter.read(
            OperationalRead(
                capability="sales",
                catalogue_revision=1,
                mapping_revision=1,
                start=datetime(2026, 10, 24, tzinfo=UTC),
                end=datetime(2026, 10, 26, tzinfo=UTC),
            )
        )


def test_fact_partition_cannot_contradict_catalogue_partition(source):
    payload = source.mapping.model_dump()
    payload["stock"]["row_scopes"][0]["value"] = "B"
    with pytest.raises(SourceMappingError, match="mapping_partition_product_mismatch"):
        validate_operational_mapping(
            OperationalMapping.model_validate(payload),
            source.discovery,
            source.catalogue.mapping,
        )


def test_branch_and_warehouse_filters_remain_source_scoped(source):
    row = read(source, "stock", branch="branch-west", warehouse="warehouse-A").rows[0]
    assert row.branch == "branch-west" and row.warehouse == "warehouse-A"
    assert read(source, "stock", branch="other-branch").status == "not_found"
    assert read(source, "stock", warehouse="other-warehouse").status == "not_found"


def test_financial_totals_cover_all_facts_before_display_limit(source):
    with source.engine.begin() as connection:
        connection.execute(
            source.tables[0].insert(), dict(tenant="A", code="002", label="Another")
        )
        connection.execute(
            source.tables[3].insert(),
            dict(
                id=4,
                tenant="A",
                product="002",
                location="west",
                unit="kg",
                transaction="ticket1",
                quantity=3,
                at=NOW,
                state="completed",
                category="approved-class",
                gross=30,
                net=25,
                currency="USD",
            ),
        )
    report = read(source, "revenue", limit=1)
    assert report.truncated and len(report.rows) == 1
    money = next(total for total in report.totals if total.currency == "USD")
    quantity = next(total for total in report.totals if total.unit == "kg")
    assert (
        money.gross_revenue == 50
        and money.net_revenue == 34
        and money.transaction_count == 1
    )
    assert (
        quantity.gross_quantity == 5
        and quantity.net_quantity == 4
        and quantity.transaction_count == 1
    )


def test_header_total_cannot_be_repeated_on_each_line(source):
    payload = source.mapping.model_dump()
    # A reviewed many-to-one join does not make a header amount additive at line grain.
    payload["sales"]["joins"] = [
        dict(
            target_object_id="o2",
            fact_columns=[source.tables[3].c.id.name],
            target_columns=[source.tables[2].c.id.name],
        )
    ]
    payload["revenue"]["gross_amount"] = dict(
        object_id="o2", column=source.tables[2].c.amount.name
    )
    with pytest.raises(SourceMappingError, match="mapping_value_not_at_fact_grain"):
        validate_operational_mapping(
            OperationalMapping.model_validate(payload),
            source.discovery,
            source.catalogue.mapping,
        )


@pytest.mark.parametrize("field", ["on_hand", "reserved", "available"])
def test_stock_quantities_must_be_at_balance_fact_grain(source, field):
    payload = source.mapping.model_dump()
    payload["stock"]["joins"] = [
        dict(
            target_object_id="o2",
            fact_columns=[source.tables[1].c.id.name],
            target_columns=[source.tables[2].c.id.name],
        )
    ]
    payload["stock"][field] = dict(
        object_id="o2", column=source.tables[2].c.amount.name
    )
    if field == "available":
        payload["stock"]["availability_rule"] = "source"
    with pytest.raises(SourceMappingError, match="mapping_value_not_at_fact_grain"):
        validate_operational_mapping(
            OperationalMapping.model_validate(payload),
            source.discovery,
            source.catalogue.mapping,
        )


@pytest.mark.parametrize(
    "signature", ["datetimeoffset", "timestamp with time zone", None]
)
def test_local_wallclock_mapping_cannot_use_aware_or_unknown_columns(source, signature):
    objects = list(source.discovery.objects)
    obj = objects[3]
    objects[3] = obj.model_copy(
        update={
            "columns": tuple(
                c.model_copy(update={"type_signature": signature})
                if c.name == source.mapping.sales.occurred_at.column
                else c
                for c in obj.columns
            )
        }
    )
    discovery = source.discovery.model_copy(update={"objects": tuple(objects)})
    mapping = source.mapping.model_copy(
        update={
            "schema_fingerprint": discovery.schema_fingerprint,
            "sales": source.mapping.sales.model_copy(
                update={"timestamp_basis": "source_local"}
            ),
        }
    )
    with pytest.raises(
        SourceMappingError, match="mapping_local_timestamp_type_unsupported"
    ):
        validate_operational_mapping(mapping, discovery, source.catalogue.mapping)


def test_approved_negative_source_signs_are_normalized_once(source):
    with source.engine.begin() as connection:
        for table in source.tables[3:]:
            connection.execute(
                table.update()
                .where(table.c.tenant == "A")
                .values(
                    quantity=-table.c.quantity, gross=-table.c.gross, net=-table.c.net
                )
            )
    payload = source.mapping.model_dump()
    payload["sales"]["quantity_multiplier"] = -1
    payload["revenue"]["amount_multiplier"] = -1
    payload["refunds"].update(quantity_multiplier=-1, amount_multiplier=-1)
    source.adapter.mapping = OperationalMapping.model_validate(payload)
    row = read(source, "revenue").rows[0]
    assert (
        row.gross_quantity == 2 and row.returned_quantity == 1 and row.net_quantity == 1
    )
    assert row.gross_revenue == 20 and row.net_revenue == 9 and row.refunds == 9


def test_latest_effective_price_needs_explicit_precedence(source):
    with source.engine.begin() as connection:
        connection.execute(
            source.tables[2].insert(),
            dict(
                id=5,
                tenant="A",
                product="001",
                location="west",
                unit="kg",
                branch="branch-west",
                warehouse="warehouse-A",
                amount=2,
                currency="USD",
                list="retail",
                starts=NOW - timedelta(hours=1),
                ends=NOW + timedelta(hours=1),
            ),
        )
    from app.schemas.source_mapping import CatalogueRequest

    key = (
        source.catalogue.search(CatalogueRequest(browse=True))
        .items[0]
        .external_product_id
    )
    assert read(source, "prices", product_id=key, as_of=NOW).status == "ambiguous"
    source.adapter.mapping = source.mapping.model_copy(
        update={
            "prices": source.mapping.prices.model_copy(
                update={"precedence": "latest_effective"}
            )
        }
    )
    report = read(source, "prices", product_id=key, as_of=NOW)
    assert report.status == "ok" and report.rows[0].price == 2


@pytest.mark.parametrize(
    "signature", ["date", "datetime", "timestamp without time zone", None]
)
def test_ambiguous_effective_price_time_basis_rejected(source, signature):
    objects = list(source.discovery.objects)
    columns = list(objects[2].columns)
    field = source.mapping.prices.valid_from.column
    columns = [
        c.model_copy(update={"type_signature": signature}) if c.name == field else c
        for c in columns
    ]
    objects[2] = objects[2].model_copy(update={"columns": tuple(columns)})
    discovery = source.discovery.model_copy(update={"objects": tuple(objects)})
    mapping = source.mapping.model_copy(
        update={"schema_fingerprint": discovery.schema_fingerprint}
    )
    with pytest.raises(
        SourceMappingError,
        match="mapping_(date_only_time_semantics|price_timezone)_unsupported",
    ):
        validate_operational_mapping(mapping, discovery, source.catalogue.mapping)
