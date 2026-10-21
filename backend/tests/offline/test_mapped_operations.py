"""Execute production compilers on memory-only SQLAlchemy sources.

Native dialect compilation is covered separately; SQLite is not evidence of
native role grants, SQL Server ODBC behaviour or native Decimal storage.
"""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from app.integrations.discovery import SourceMappingError
from app.integrations.mapped_operational import MappedOperationalSource, compile_facts
from app.integrations.mapped_products import MappedProductSource
from app.schemas.operational_mapping import OperationalMapping, OperationalRead
from app.schemas.source_mapping import CatalogueRequest, ProductMapping, SchemaDiscovery
from pydantic import ValidationError
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Table,
    create_engine,
)
from sqlalchemy.dialects import mssql, postgresql
from sqlalchemy.pool import StaticPool

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


def fixture_source(layout=0, engine=None, read_engine=None):
    engine = engine or create_engine(
        "sqlite+pysqlite:///:memory:", poolclass=StaticPool
    )
    read_engine = read_engine or engine
    schema = (
        "main"
        if engine.dialect.name == "sqlite"
        else "dbo"
        if engine.dialect.name == "mssql"
        else "public"
    )
    meta = MetaData(schema=None if engine.dialect.name == "sqlite" else schema)
    prefix = f"variant_{layout}_"
    names = {
        key: prefix + key for key in ("products", "stock", "prices", "sales", "refunds")
    }
    products = Table(
        names["products"],
        meta,
        Column("tenant", String(128), primary_key=True),
        Column("code", String(128), primary_key=True),
        Column("label", String, nullable=False),
    )
    tables = [products]
    for kind in ("stock", "prices", "sales", "refunds"):
        common = [
            Column("id", Integer, primary_key=True),
            Column("tenant", String, nullable=False),
            Column("product", String, nullable=False),
            Column("location", String),
            Column("branch", String),
            Column("warehouse", String),
            Column("unit", String, nullable=False),
        ]
        if kind == "stock":
            extra = [
                Column(n, Numeric(38, 18))
                for n in ("balance", "reserved", "threshold", "target")
            ]
        elif kind == "prices":
            extra = [
                Column("amount", Numeric(38, 18)),
                Column("currency", String),
                Column("list", String),
                Column("starts", DateTime(timezone=True)),
                Column("ends", DateTime(timezone=True)),
            ]
        else:
            extra = [
                Column("transaction", String),
                Column("quantity", Numeric(38, 18)),
                Column("at", DateTime(timezone=True)),
                Column("state", String),
                Column("category", String),
                Column("gross", Numeric(38, 18)),
                Column("net", Numeric(38, 18)),
                Column("currency", String),
            ]
        keys = (
            ("id", "tenant", "product", "location", "unit")
            if layout % 2 and kind in {"sales", "refunds"}
            else ("id",)
        )
        for column in common:
            column.primary_key = False
            if column.name in keys:
                column.nullable = False
                if isinstance(column.type, String):
                    column.type = String(128)
        tables.append(
            Table(
                names[kind],
                meta,
                *common,
                *extra,
                PrimaryKeyConstraint(
                    *(column for column in common if column.name in keys)
                ),
            )
        )
    if layout % 2:
        for table in tables:
            for column in table.c:
                column.name = f"field_{layout}_{column.name}"
    meta.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            products.insert(),
            [dict(tenant=t, code="001", label="Random Diet 7 UP") for t in ("A", "B")],
        )
        for table, kind in zip(
            tables[1:], ("stock", "prices", "sales", "refunds"), strict=True
        ):
            base = dict(
                id=1,
                tenant="A",
                product="001",
                location="west",
                unit="kg",
                branch="branch-west",
                warehouse="warehouse-A",
            )
            if kind == "stock":
                base.update(
                    balance=Decimal("-2.1250"),
                    reserved=Decimal("3.0000"),
                    threshold=4,
                    target=10,
                )
            elif kind == "prices":
                base.update(
                    amount=Decimal("1.1234"),
                    currency="USD",
                    list="retail",
                    starts=NOW - timedelta(days=1),
                    ends=NOW + timedelta(days=1),
                )
            else:
                base.update(
                    transaction="ticket1",
                    quantity=Decimal(1 if kind == "refunds" else 2),
                    at=NOW,
                    state="completed",
                    category="approved-class",
                    gross=20,
                    net=9 if kind == "refunds" else 18,
                    currency="USD",
                )
            connection.execute(table.insert(), [base, {**base, "id": 2, "tenant": "B"}])
    objects = []
    for i, table in enumerate(tables):
        objects.append(
            dict(
                object_id=f"o{i}",
                schema_name=schema or "main",
                name=table.name,
                columns=[
                    dict(
                        name=c.name,
                        kind="integer"
                        if isinstance(c.type, Integer)
                        else "decimal"
                        if isinstance(c.type, Numeric)
                        else "date"
                        if isinstance(c.type, DateTime)
                        else "text",
                        nullable=c.nullable,
                        type_signature=(
                            "datetimeoffset"
                            if engine.dialect.name == "mssql"
                            else "timestamp with time zone"
                        )
                        if isinstance(c.type, DateTime)
                        else None,
                    )
                    for c in table.c
                ],
                unique_keys=[tuple(c.name for c in table.primary_key)],
            )
        )
    discovery = SchemaDiscovery(
        engine="sqlserver" if engine.dialect.name == "mssql" else "postgresql",
        source_fingerprint="a" * 64,
        objects=objects,
    )
    product_mapping = ProductMapping(
        capability="products",
        object_id="o0",
        key_columns=(products.c.tenant.name, products.c.code.name),
        name_columns=(products.c.label.name,),
        row_scopes=({"column": products.c.tenant.name, "value": "A"},),
    )

    def ref(i, col):
        return dict(object_id=f"o{i}", column=tables[i].c[col].name)

    def fact(i):
        return dict(
            object_id=f"o{i}",
            key_columns=[column.name for column in tables[i].primary_key],
            product_columns=[tables[i].c.tenant.name, tables[i].c.product.name],
            row_scopes=[dict(field=ref(i, "tenant"), value="A")],
            location=ref(i, "location"),
            branch=ref(i, "branch"),
            warehouse=ref(i, "warehouse"),
            unit=ref(i, "unit"),
        )

    def sales(i):
        return dict(
            **fact(i),
            transaction_columns=[ref(i, "transaction")],
            transaction_identity="reviewed_denormalized_key",
            quantity=ref(i, "quantity"),
            occurred_at=ref(i, "at"),
            status=ref(i, "state"),
            completed_statuses=["completed"],
            timestamp_basis="utc",
            category=ref(i, "category"),
        )

    mapping = OperationalMapping(
        business_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        catalogue_revision=1,
        source_fingerprint=discovery.source_fingerprint,
        schema_fingerprint=discovery.schema_fingerprint,
        timezone="Asia/Beirut",
        external_scope="partitioned",
        scope_reviewed=True,
        stock=dict(
            **fact(1),
            on_hand=ref(1, "balance"),
            reserved=ref(1, "reserved"),
            reservations_complete=True,
            availability_rule="subtract_complete_reservations",
            reorder_point=ref(1, "threshold"),
            reorder_trigger="below",
            restock_basis="available",
            target_quantity=ref(1, "target"),
        ),
        prices=dict(
            **fact(2),
            amount=ref(2, "amount"),
            currency=ref(2, "currency"),
            price_list=ref(2, "list"),
            valid_from=ref(2, "starts"),
            valid_until=ref(2, "ends"),
            tax_basis="excluded",
        ),
        sales={**sales(3), "returns_semantics": "separate_complete"},
        revenue=dict(
            gross_amount=ref(3, "gross"),
            net_amount=ref(3, "net"),
            currency=ref(3, "currency"),
            net_basis="before_refunds",
            tax_basis="excluded",
        ),
        refunds=dict(
            **sales(4),
            tax_basis="excluded",
            original_transaction_columns=[ref(4, "transaction")],
            amount=ref(4, "net"),
            currency=ref(4, "currency"),
        ),
    )

    class Connector:
        config = SimpleNamespace(query_timeout_seconds=2)

        @contextmanager
        def connection(self):
            with read_engine.connect() as connection:
                yield connection

        def assert_schema(self, current, _connection):
            if current != discovery:
                raise SourceMappingError("mapping_schema_changed")

    catalogue = MappedProductSource(Connector(), discovery, product_mapping)
    adapter = MappedOperationalSource(
        catalogue,
        mapping,
        1,
        business_id=mapping.business_id,
        source_id=mapping.source_id,
    )
    return SimpleNamespace(
        engine=engine,
        tables=tables,
        catalogue=catalogue,
        adapter=adapter,
        mapping=mapping,
        discovery=discovery,
    )


@pytest.fixture(params=[0, 49])
def source(request):
    value = fixture_source(request.param)
    yield value
    value.engine.dispose()


def read(source, capability, **kwargs):
    return source.adapter.read(
        OperationalRead(
            capability=capability,
            catalogue_revision=1,
            mapping_revision=1,
            start=NOW - timedelta(days=1),
            end=NOW + timedelta(days=1),
            **kwargs,
        )
    )


@pytest.mark.parametrize(
    "capability",
    [
        "stock",
        "availability",
        "restocking",
        "sales",
        "revenue",
        "refunds",
        "best_sellers",
    ],
)
def test_real_queries_are_partitioned_and_preserve_semantics(source, capability):
    output = read(source, capability)
    assert output.status == "ok" and len(output.rows) == 1
    assert output.rows[0].unit == "kg"
    row = output.rows[0]
    if capability in {"stock", "availability", "restocking"}:
        assert row.on_hand == Decimal("-2.1250") and row.available == Decimal("-5.1250")
        if capability == "restocking":
            assert row.recommended_quantity == Decimal("15.1250")
    else:
        assert (
            row.gross_quantity == 2
            and row.returned_quantity == 1
            and row.net_quantity == 1
        )
        assert (
            row.transaction_count == 1
            and row.gross_revenue == 20
            and row.net_revenue == 9
            and row.refunds == 9
        )


def test_current_price_is_not_receipt_price_and_scopes_are_explicit(source):
    product_id = (
        source.catalogue.search(CatalogueRequest(browse=True))
        .items[0]
        .external_product_id
    )
    row = read(source, "prices", product_id=product_id, as_of=NOW).rows[0]
    assert (
        row.price == Decimal("1.1234")
        and row.currency == "USD"
        and row.price_list == "retail"
        and row.tax_basis == "excluded"
    )
    assert (
        read(
            source, "prices", product_id=product_id, as_of=NOW + timedelta(days=2)
        ).status
        == "not_found"
    )
    assert (
        read(
            source, "prices", product_id=product_id, as_of=NOW, price_list="missing"
        ).status
        == "not_found"
    )


@pytest.mark.parametrize("capability", ["prices", "revenue", "refunds"])
def test_reviewed_currency_codes_translate_and_unknown_codes_fail(source, capability):
    product_id = (
        source.catalogue.search(CatalogueRequest(browse=True))
        .items[0]
        .external_product_id
    )
    payload = source.mapping.model_dump(mode="json")
    for name in ("prices", "revenue", "refunds"):
        payload[name]["currency_codes"] = {"1": "LBP"}
    source.adapter.mapping = OperationalMapping.model_validate(payload)
    for table in source.tables[2:]:
        with source.engine.begin() as connection:
            connection.execute(table.update().values(currency="1"))
    arguments = (
        {"product_id": product_id, "as_of": NOW} if capability == "prices" else {}
    )
    report = read(source, capability, **arguments)
    assert report.rows and all(row.currency == "LBP" for row in report.rows)
    table = source.tables[
        2 if capability == "prices" else 3 if capability == "revenue" else 4
    ]
    with source.engine.begin() as connection:
        connection.execute(
            table.update().where(table.c.tenant == "A").values(currency="9")
        )
    with pytest.raises(SourceMappingError, match="operational_text_invalid"):
        read(source, capability, **arguments)


def test_empty_currency_translation_preserves_existing_mapping_serialization(source):
    payload = source.mapping.model_dump(mode="json")
    for name in ("prices", "revenue", "refunds"):
        assert "currency_codes" not in payload[name]
        payload[name]["currency_codes"] = {}
    assert OperationalMapping.model_validate(payload).model_dump(
        mode="json"
    ) == source.mapping.model_dump(mode="json")


def test_transaction_count_totals_do_not_require_money_or_quantity(source):
    payload = source.mapping.model_dump(mode="json")
    for name in ("stock", "prices", "revenue", "refunds"):
        payload[name] = None
    sales = payload["sales"]
    sales.update(
        grain="transaction_events",
        product_columns=[],
        product_reference="none",
        quantity=None,
        unit=None,
        returns_semantics="unsupported",
        transaction_identity="enforced_header_key",
        transaction_columns=[
            {"object_id": sales["object_id"], "column": name}
            for name in sales["key_columns"]
        ],
    )
    source.adapter.mapping = OperationalMapping.model_validate(payload)
    report = read(source, "sales")
    assert report.rows[0].transaction_count == 1
    assert len(report.totals) == 1 and report.totals[0].transaction_count == 1
    assert report.totals[0].gross_quantity is None
    assert report.totals[0].gross_revenue is None


@pytest.mark.parametrize(
    "codes",
    [{"1": "LL"}, {"1": "lbp"}, {"": "LBP"}, {str(i): "USD" for i in range(17)}],
)
def test_currency_translation_rejects_invalid_or_unbounded_contracts(source, codes):
    payload = source.mapping.model_dump(mode="json")
    payload["prices"]["currency_codes"] = codes
    with pytest.raises(ValidationError):
        OperationalMapping.model_validate(payload)


@pytest.mark.parametrize("dialect", [postgresql.dialect(), mssql.dialect()])
@pytest.mark.parametrize("capability", ["stock", "prices", "sales", "refunds"])
def test_native_dialect_compilation_remains_bound(source, dialect, capability):
    request = OperationalRead(
        capability=capability,
        catalogue_revision=1,
        mapping_revision=1,
        product_id=source.catalogue.search(CatalogueRequest(browse=True))
        .items[0]
        .external_product_id,
        start=NOW - timedelta(days=1),
        end=NOW + timedelta(days=1),
        as_of=NOW,
        location="x'); DROP TABLE x;--",
    )
    statement, parameters = compile_facts(
        source.mapping,
        source.discovery,
        source.catalogue.mapping,
        getattr(source.mapping, capability),
        request,
    )
    sql = str(statement.compile(dialect=dialect))
    assert request.location not in sql and request.location in parameters.values()
    assert "scope_0" in parameters and (
        "LIMIT" in sql or "ROW_NUMBER" in sql or "TOP" in sql
    )


@pytest.mark.parametrize(
    "field,value", [("mapping_revision", 2), ("catalogue_revision", 2)]
)
def test_stale_revisions_fail_before_queries(source, field, value):
    request = OperationalRead(
        capability="stock", catalogue_revision=1, mapping_revision=1
    ).model_copy(update={field: value})
    with pytest.raises(SourceMappingError, match="mapping_selection_stale"):
        source.adapter.read(request)


def test_external_product_selection_cannot_cross_partition(source):
    from app.integrations.mapped_products import encode_product_key

    with pytest.raises(SourceMappingError, match="operational_product_not_found"):
        read(source, "stock", product_id=encode_product_key(("B", "001")))


def test_source_business_and_identifier_scope_are_mandatory(source):
    with pytest.raises(SourceMappingError, match="mapping_scope_mismatch"):
        MappedOperationalSource(
            source.catalogue,
            source.mapping,
            1,
            business_id=uuid.uuid4(),
            source_id=source.mapping.source_id,
        )
    with pytest.raises(ValidationError):
        OperationalMapping.model_validate(
            {
                **source.mapping.model_dump(),
                "stock": {**source.mapping.stock.model_dump(), "row_scopes": []},
            }
        )


def test_ambiguous_price_records_are_not_arbitrarily_selected(source):
    with source.engine.begin() as connection:
        connection.execute(
            source.tables[2].insert(),
            dict(
                id=3,
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
    key = (
        source.catalogue.search(CatalogueRequest(browse=True))
        .items[0]
        .external_product_id
    )
    assert read(source, "prices", product_id=key, as_of=NOW).status == "ambiguous"


def test_unsupported_is_not_zero(source):
    source.adapter.mapping = source.mapping.model_copy(update={"prices": None})
    key = (
        source.catalogue.search(CatalogueRequest(browse=True))
        .items[0]
        .external_product_id
    )
    result = read(source, "prices", product_id=key, as_of=NOW)
    assert result.status == "unsupported" and result.rows == ()


def test_no_double_subtraction_when_source_amount_already_net(source):
    source.adapter.mapping = source.mapping.model_copy(
        update={
            "revenue": source.mapping.revenue.model_copy(
                update={"net_basis": "already_net_of_refunds"}
            )
        }
    )
    assert read(source, "revenue").rows[0].net_revenue == 18


def test_cancelled_documents_are_excluded(source):
    with source.engine.begin() as connection:
        connection.execute(
            source.tables[3]
            .update()
            .where(source.tables[3].c.tenant == "A")
            .values(state="cancelled")
        )
    row = read(source, "sales").rows[0]
    assert row.gross_quantity == 0 and row.net_quantity == -1 and row.net_revenue == -9


def test_reporting_bounds_are_required_and_bounded():
    for extra in (
        {},
        dict(start=NOW, end=NOW),
        dict(start=NOW, end=NOW + timedelta(days=367)),
    ):
        with pytest.raises(ValidationError):
            OperationalRead(
                capability="sales", catalogue_revision=1, mapping_revision=1, **extra
            )
