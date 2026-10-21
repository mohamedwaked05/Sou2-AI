"""Native restricted ODBC execution at compatibility level 100."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.integrations.discovery import EngineConnector, SourceConnection
from app.integrations.operational_quality import (
    evaluate_operational,
    execution_discovery,
    family_mapping,
)
from app.schemas.operational_mapping import OperationalMapping, OperationalRead
from app.schemas.source_mapping import ProductMapping
from app.services.capabilities import _adapter
from pydantic import SecretStr
from sqlalchemy import text
from tests.operational_isolated.test_sqlserver_operations import (
    sqlserver_source as sqlserver_source,
)


@pytest.fixture(scope="module")
def source(sqlserver_source):
    fixture = sqlserver_source[0][0]
    with fixture.catalogue.connector.connection() as connection:
        reader_url = connection.engine.url.render_as_string(hide_password=False)
    connector = EngineConnector(
        SourceConnection(
            key="capability_sql",
            display_name="Synthetic capability SQL",
            engine="sqlserver",
            url=SecretStr(reader_url),
            business_ids=(uuid.uuid4(),),
            discovery_scope=tuple(
                dict(
                    schema_name="dbo",
                    object_name=t.name,
                    columns=tuple(c.name for c in t.c),
                )
                for t in fixture.tables
            ),
        )
    )
    discovery = connector.discover()
    ids = {
        a.object_id: b.object_id
        for a, b in zip(fixture.discovery.objects, discovery.objects, strict=True)
    }

    def translate(value):
        if isinstance(value, dict):
            return {
                k: ids.get(v, v)
                if k.endswith("object_id") and isinstance(v, str)
                else translate(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [translate(v) for v in value]
        return value

    product = ProductMapping.model_validate(
        translate(fixture.catalogue.mapping.model_dump(mode="json"))
    )
    payload = translate(fixture.mapping.model_dump(mode="json"))
    payload.update(
        source_fingerprint=discovery.source_fingerprint,
        schema_fingerprint=discovery.schema_fingerprint,
    )
    mapping = OperationalMapping.model_validate(payload)
    database = fixture.engine.url.database
    with fixture.engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as connection:
        connection.execute(
            text(f"ALTER DATABASE [{database}] SET COMPATIBILITY_LEVEL=100")
        )
    with fixture.engine.begin() as connection:
        connection.execute(
            fixture.tables[0]
            .update()
            .where(fixture.tables[0].c.tenant == "A")
            .values(label="")
        )
    yield fixture, connector, discovery, product, mapping
    connector.engine.dispose()


@pytest.mark.parametrize(
    "family,operation",
    [
        ("inventory", "stock"),
        ("pricing", "prices"),
        ("sales", "sales"),
        ("financial", "revenue"),
    ],
)
def test_sqlserver_independent_full_scope_quality_and_execution(
    source, family, operation
):
    fixture, connector, discovery, products, mapping = source
    selected = family_mapping(mapping, family)
    dependencies = execution_discovery(selected, discovery, products)
    selected = selected.model_copy(
        update={"schema_fingerprint": dependencies.schema_fingerprint}
    )
    adapter = _adapter(connector, selected, dependencies, products, 1)
    try:
        with connector.connection(consistent=True) as connection:
            quality = evaluate_operational(adapter, connection)
        assert quality.invalid == 0 and quality.total == (
            2 if family in {"sales", "financial"} else 1
        )
        now = datetime(2026, 10, 15, 12, tzinfo=UTC)
        from app.integrations.mapped_products import encode_product_key

        request = OperationalRead(
            capability=operation,
            catalogue_revision=1,
            mapping_revision=1,
            start=now - timedelta(days=1),
            end=now + timedelta(days=1),
            product_id=encode_product_key(("A", "001"))
            if operation == "prices"
            else None,
            as_of=now if operation == "prices" else None,
        )
        result = adapter.read(request)
        assert result.status == "ok" and result.rows
    except Exception as exc:
        from app.integrations.discovery import SourceMappingError

        if isinstance(exc, SourceMappingError):
            raise exc.__context__ or exc from None
        raise


def test_sqlserver_bad_financial_record_is_counted_and_not_omitted(source):
    fixture, connector, discovery, products, mapping = source
    selected = family_mapping(mapping, "financial")
    dependencies = execution_discovery(selected, discovery, products)
    selected = selected.model_copy(
        update={"schema_fingerprint": dependencies.schema_fingerprint}
    )
    adapter = _adapter(connector, selected, dependencies, products, 1)
    sales = fixture.tables[3]
    with fixture.engine.begin() as connection:
        connection.execute(sales.update().where(sales.c.tenant == "A").values(net=None))
    try:
        with connector.connection(consistent=True) as connection:
            result = evaluate_operational(adapter, connection)
        assert (
            result.invalid == 1
            and result.total == 2
            and result.reasons["invalid_amount"] == 1
        )
        from app.integrations.discovery import SourceMappingError

        with pytest.raises(SourceMappingError, match="capability_data_quality_changed"):
            now = datetime(2026, 10, 15, 12, tzinfo=UTC)
            adapter.read(
                OperationalRead(
                    capability="revenue",
                    catalogue_revision=1,
                    mapping_revision=1,
                    start=now - timedelta(days=1),
                    end=now + timedelta(days=1),
                )
            )
    finally:
        with fixture.engine.begin() as connection:
            connection.execute(
                sales.update().where(sales.c.tenant == "A").values(net=18)
            )


def test_sqlserver_currency_translation_checks_every_in_scope_price(source):
    from app.integrations.mapped_products import encode_product_key

    fixture, connector, discovery, products, mapping = source
    payload = family_mapping(mapping, "pricing").model_dump(mode="json")
    payload["prices"]["currency_codes"] = {"1": "LBP"}
    payload["prices"]["product_reference"] = "fact_identity"
    selected = OperationalMapping.model_validate(payload)
    dependencies = execution_discovery(selected, discovery, None)
    selected = selected.model_copy(
        update={"schema_fingerprint": dependencies.schema_fingerprint}
    )
    adapter = _adapter(connector, selected, dependencies, None, 1)
    prices = fixture.tables[2]
    with fixture.engine.begin() as connection:
        connection.execute(prices.update().values(currency="1"))
    try:
        with connector.connection(consistent=True) as connection:
            quality = evaluate_operational(adapter, connection)
        assert quality.invalid == 0
        result = adapter.read(
            OperationalRead(
                capability="prices",
                catalogue_revision=1,
                mapping_revision=1,
                product_id=encode_product_key(("A", "001")),
                as_of=datetime(2026, 10, 15, 12, tzinfo=UTC),
            )
        )
        assert result.rows and all(row.currency == "LBP" for row in result.rows)
        with fixture.engine.begin() as connection:
            connection.execute(
                prices.update().where(prices.c.tenant == "A").values(currency="9")
            )
        with connector.connection(consistent=True) as connection:
            quality = evaluate_operational(adapter, connection)
        assert quality.invalid == 1 and quality.reasons["invalid_currency"] == 1
    finally:
        with fixture.engine.begin() as connection:
            connection.execute(prices.update().values(currency="USD"))


def test_sqlserver_header_count_without_product_join_or_quantity(source):
    _, connector, discovery, _, mapping = source
    payload = family_mapping(mapping, "sales").model_dump(mode="json")
    payload["refunds"] = None
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
    selected = OperationalMapping.model_validate(payload)
    dependencies = execution_discovery(selected, discovery, None)
    selected = selected.model_copy(
        update={"schema_fingerprint": dependencies.schema_fingerprint}
    )
    adapter = _adapter(connector, selected, dependencies, None, 1)
    with connector.connection(consistent=True) as connection:
        quality = evaluate_operational(adapter, connection)
    assert quality.total == 1 and quality.invalid == 0
    now = datetime(2026, 10, 15, 12, tzinfo=UTC)
    report = adapter.read(
        OperationalRead(
            capability="sales",
            catalogue_revision=1,
            mapping_revision=1,
            start=now - timedelta(days=1),
            end=now + timedelta(days=1),
        )
    )
    assert sum(total.transaction_count for total in report.totals) == 1
