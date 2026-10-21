"""Real ODBC 18 execution against a fresh SQL Server, never a business database."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.integrations.catalogue_quality import (
    RULE_EXPLANATIONS,
    evaluate_quality,
    mapping_digest,
)
from app.integrations.discovery import (
    EngineConnector,
    SourceConnection,
    SourceMappingError,
)
from app.integrations.mapped_products import MappedProductSource
from app.schemas.catalogue_quality import CatalogueCoverage, ExclusionPolicy
from app.schemas.source_mapping import CatalogueRequest, ProductMapping
from pydantic import SecretStr
from sqlalchemy import Column, MetaData, String, Table, text
from tests.operational_isolated.test_sqlserver_operations import (
    sqlserver_source as sqlserver_source,
)


@pytest.fixture(scope="module")
def catalogue(sqlserver_source):
    sources, _ = sqlserver_source
    admin = sources[0].engine
    with sources[0].catalogue.connector.connection() as connection:
        reader_url = connection.engine.url.render_as_string(hide_password=False)
    metadata = MetaData(schema="dbo")
    product = Table(
        "quality_items",
        metadata,
        Column("id", String(128), unique=True),
        Column("name", String(300)),
        Column("private_field", String(128)),
    )
    metadata.create_all(admin)
    with admin.begin() as connection:
        connection.execute(
            product.insert(),
            [
                {"id": f"p{i:04d}", "name": "" if i < 10 else f"Product {i}"}
                for i in range(1000)
            ],
        )
        connection.execute(
            text("GRANT SELECT (id,name) ON dbo.quality_items TO fixture_reader")
        )
    connector = EngineConnector(
        SourceConnection(
            key="quality_sqlserver",
            display_name="Quality SQL Server",
            engine="sqlserver",
            business_ids=(uuid.uuid4(),),
            url=SecretStr(reader_url),
            discovery_scope=(
                {
                    "schema_name": "dbo",
                    "object_name": "quality_items",
                    "columns": ("id", "name"),
                },
            ),
        )
    )
    discovery = connector.discover()
    mapping = ProductMapping(
        capability="products",
        object_id="o1",
        key_columns=("id",),
        name_columns=("name",),
    )
    yield admin, product, MappedProductSource(connector, discovery, mapping)
    connector.engine.dispose()


def evaluate(adapter):
    try:
        with adapter.connector.connection(consistent=True) as connection:
            return evaluate_quality(adapter, connection)
    except SourceMappingError as exc:
        # Disposable-only fixture data permits the native compiler diagnostic.
        raise exc.__context__ or exc from None


def test_sqlserver_unrelated_hidden_metadata_does_not_block_catalogue(catalogue):
    _, _, adapter = catalogue
    result = evaluate(adapter)
    assert result.total == 1000 and result.invalid == 10
    assert result.impact == "SAFE_WITH_EXCLUSIONS"


def test_sqlserver_native_safe_exclusion_and_null_identity(catalogue):
    admin, product, adapter = catalogue
    with admin.begin() as connection:
        connection.execute(
            product.update().where(product.c.id == "p0999").values(id=None)
        )
    result = evaluate(adapter)
    assert result.total == 1000 and result.invalid == 11
    assert (
        result.reasons["missing_name"] == 10
        and result.reasons["invalid_identifier"] == 1
    )
    assert result.impact == "SAFE_WITH_EXCLUSIONS"
    assert any(row.identifier is None for row in result.diagnostics)
    partial = MappedProductSource(
        adapter.connector,
        adapter.discovery,
        adapter.mapping,
        quality_policy=ExclusionPolicy(
            rules=tuple(RULE_EXPLANATIONS),
            mapping_digest=mapping_digest(adapter.mapping),
            data_digest=result.data_digest,
            relationship_digest=result.relationship_digest,
        ),
        coverage=CatalogueCoverage(
            approval_type="partial",
            excluded_records=11,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )
    page = partial.search(CatalogueRequest(browse=True, limit=50))
    assert len(page.items) == 50 and page.items[0].external_product_id == "p0010"
    assert page.continuation and page.coverage.excluded_records == 11
    assert not partial.search(
        CatalogueRequest(external_product_id="p0000", mapping_version=1)
    ).items
    with admin.begin() as connection:
        connection.execute(
            product.update().where(product.c.id == "p0010").values(name="changed")
        )
    with pytest.raises(SourceMappingError, match="catalogue_partial_data_changed"):
        partial.search(CatalogueRequest(query="Product"))


def test_sqlserver_reader_still_has_no_source_write_permissions(catalogue):
    _, _, adapter = catalogue
    with adapter.connector.connection() as connection:
        for permission in ("INSERT", "UPDATE", "DELETE", "ALTER"):
            assert (
                connection.scalar(
                    text(
                        "SELECT "
                        "HAS_PERMS_BY_NAME('dbo.quality_items','OBJECT',:permission)"
                    ),
                    {"permission": permission},
                )
                == 0
            )


def test_sqlserver_legacy_database_compatibility_uses_the_same_safe_digest(catalogue):
    admin, _, adapter = catalogue
    before = evaluate(adapter)
    database = admin.url.database
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(
            text(f"ALTER DATABASE [{database}] SET COMPATIBILITY_LEVEL=100")
        )
    after = evaluate(adapter)
    assert after.data_digest == before.data_digest
    assert after.total == before.total and after.invalid == before.invalid
