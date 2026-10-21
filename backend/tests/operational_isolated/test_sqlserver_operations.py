"""Synthetic SQL Server only; the runner shares a new network-none container."""

import importlib.util
import os
import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL


@pytest.fixture(scope="module")
def sqlserver_source():
    nonce = os.environ.get("SOU2AI_SQLSERVER_FIXTURE", "")
    assert re.fullmatch(r"[0-9a-f]{32}", nonce), "Disposable launcher required"
    # Throwaway fixture credentials, never deployment credentials.
    password = "SyntheticOnly-NotASecret-49!"

    def url(user, secret, database):
        return URL.create(
            "mssql+pyodbc",
            username=user,
            password=secret,
            host="127.0.0.1",
            port=1433,
            database=database,
            query={
                "driver": "ODBC Driver 18 for SQL Server",
                "Encrypt": "yes",
                "TrustServerCertificate": "yes",
            },
        )

    master = create_engine(
        url("sa", password, "master"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )
    with master.connect() as connection:
        assert set(connection.scalars(text("SELECT name FROM sys.databases"))) == {
            "master",
            "model",
            "msdb",
            "tempdb",
        }
        name = f"disposable_{nonce}"
        connection.execute(text(f"CREATE DATABASE [{name}]"))
        connection.execute(
            text(
                "CREATE LOGIN fixture_reader WITH "
                "PASSWORD='SyntheticReader-NotASecret-49!', CHECK_POLICY=OFF"
            )
        )
    admin = create_engine(url("sa", password, name), hide_parameters=True)
    reader = create_engine(
        url("fixture_reader", "SyntheticReader-NotASecret-49!", name),
        hide_parameters=True,
    )
    spec = importlib.util.spec_from_file_location(
        "fixture_sqlserver", Path("tests/offline/test_mapped_operations.py")
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    sources = [fixture.fixture_source(layout, admin, reader) for layout in (900, 901)]
    with admin.begin() as connection:
        connection.execute(text("CREATE USER fixture_reader FOR LOGIN fixture_reader"))
        for table in [table for source in sources for table in source.tables]:
            connection.execute(
                text(f"GRANT SELECT ON dbo.[{table.name}] TO fixture_reader")
            )
    yield sources, fixture
    for engine in (reader, admin, master):
        engine.dispose()


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
def test_sqlserver_native_semantics(sqlserver_source, capability):
    sources, fixture = sqlserver_source
    for source in sources:
        fixture.test_real_queries_are_partitioned_and_preserve_semantics(
            source, capability
        )


def test_sqlserver_price_decimal_and_effective_dates(sqlserver_source):
    sources, fixture = sqlserver_source
    for source in sources:
        fixture.test_current_price_is_not_receipt_price_and_scopes_are_explicit(source)


def test_sqlserver_catalogue_partition_and_product_identity(sqlserver_source):
    sources, fixture = sqlserver_source
    for source in sources:
        fixture.test_external_product_selection_cannot_cross_partition(source)


def test_sqlserver_composite_fact_keys_and_decimal_precision(sqlserver_source):
    from decimal import Decimal

    sources, fixture = sqlserver_source
    source = sources[1]
    assert len(source.mapping.sales.key_columns) == 5
    assert source.discovery.engine == "sqlserver"
    value = Decimal("12345678901234567890.123456789012345678")
    with source.engine.begin() as connection:
        connection.execute(
            source.tables[1]
            .update()
            .where(source.tables[1].c.tenant == "A")
            .values(balance=value)
        )
    row = fixture.read(source, "stock").rows[0]
    assert row.on_hand == value
    assert row.available == Decimal("12345678901234567887.123456789012345678")


def test_sqlserver_allowlisted_view_metadata_and_definition_drift(sqlserver_source):
    import uuid

    from app.integrations.discovery import (
        EngineConnector,
        SourceConnection,
        SourceMappingError,
    )
    from app.integrations.mapped_products import validate_mapping
    from app.schemas.source_mapping import ProductMapping
    from pydantic import SecretStr

    sources, _ = sqlserver_source
    source = sources[0]
    product = source.tables[0]
    quote = source.engine.dialect.identifier_preparer.quote
    view = "fixture_catalogue_view"
    columns = ",".join(quote(c.name) for c in product.c)
    definition = f"SELECT {columns} FROM dbo.{quote(product.name)}"
    with source.engine.begin() as connection:
        connection.execute(text(f"CREATE VIEW dbo.{quote(view)} AS {definition}"))
        connection.execute(text(f"GRANT SELECT ON dbo.{quote(view)} TO fixture_reader"))
    with source.catalogue.connector.connection() as connection:
        reader_url = connection.engine.url.render_as_string(hide_password=False)
    connector = EngineConnector(
        SourceConnection(
            key="view_fixture",
            display_name="Synthetic view",
            engine="sqlserver",
            business_ids=(uuid.uuid4(),),
            url=SecretStr(reader_url),
            discovery_scope=(
                {
                    "schema_name": "dbo",
                    "object_name": view,
                    "columns": tuple(c.name for c in product.c),
                },
            ),
        )
    )
    discovery = connector.discover()
    assert not discovery.objects[0].unique_keys
    mapping = ProductMapping(
        capability="products",
        object_id="o1",
        key_columns=(product.c.code.name,),
        name_columns=(product.c.label.name,),
    )
    with pytest.raises(SourceMappingError):
        validate_mapping(mapping, discovery)
    with source.engine.begin() as connection:
        connection.execute(
            text(f"ALTER VIEW dbo.{quote(view)} AS {definition} WHERE 1=0")
        )
    with (
        connector.connection() as connection,
        pytest.raises(SourceMappingError, match="mapping_schema_changed"),
    ):
        connector.assert_schema(discovery, connection)
    connector.engine.dispose()


def test_sqlserver_actual_discovery_drives_both_generic_layouts(sqlserver_source):
    import uuid

    from app.integrations.discovery import EngineConnector, SourceConnection
    from app.integrations.mapped_operational import MappedOperationalSource
    from app.integrations.mapped_products import MappedProductSource
    from app.schemas.operational_mapping import OperationalMapping
    from app.schemas.source_mapping import ProductMapping
    from pydantic import SecretStr

    sources, fixture = sqlserver_source
    for source in sources:
        with source.catalogue.connector.connection() as connection:
            url = connection.engine.url.render_as_string(hide_password=False)
        connector = EngineConnector(
            SourceConnection(
                key="actual_discovery",
                display_name="Synthetic discovery",
                engine="sqlserver",
                business_ids=(uuid.uuid4(),),
                url=SecretStr(url),
                discovery_scope=tuple(
                    {
                        "schema_name": "dbo",
                        "object_name": t.name,
                        "columns": tuple(c.name for c in t.c),
                    }
                    for t in source.tables
                ),
            )
        )
        discovery = connector.discover()
        ids = {
            old.object_id: new.object_id
            for old, new in zip(
                source.discovery.objects, discovery.objects, strict=True
            )
        }

        def translate(value, ids=ids):
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

        mapping = translate(source.mapping.model_dump(mode="json"))
        mapping.update(
            source_fingerprint=discovery.source_fingerprint,
            schema_fingerprint=discovery.schema_fingerprint,
        )
        catalogue = MappedProductSource(
            connector,
            discovery,
            ProductMapping.model_validate(
                translate(source.catalogue.mapping.model_dump(mode="json"))
            ),
        )
        adapter = MappedOperationalSource(
            catalogue,
            OperationalMapping.model_validate(mapping),
            1,
            business_id=source.mapping.business_id,
            source_id=source.mapping.source_id,
        )
        for capability in (
            "stock",
            "availability",
            "sales",
            "revenue",
            "refunds",
            "best_sellers",
            "restocking",
        ):
            output = adapter.read(
                fixture.OperationalRead(
                    capability=capability,
                    catalogue_revision=1,
                    mapping_revision=1,
                    start=fixture.NOW - fixture.timedelta(days=1),
                    end=fixture.NOW + fixture.timedelta(days=1),
                )
            )
            expected = fixture.read(source, capability)
            assert output.status == expected.status and output.rows == expected.rows
        fixture.test_current_price_is_not_receipt_price_and_scopes_are_explicit(
            type("Fixture", (), {"adapter": adapter, "catalogue": catalogue})()
        )
        connector.engine.dispose()
