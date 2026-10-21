"""Source-value validation and the limits of current activation admission.

Synthetic Python records only: no source, platform, Redis or provider I/O. The
open-mode cases demonstrate an existing policy gap, not selective admission.
"""

import copy
import socket
import subprocess
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from app.core import runtime
from app.core.admission import require_open_admission
from app.core.config import Settings
from app.core.exceptions import ApplicationError
from app.integrations.discovery import SourceMappingError
from app.integrations.mapped_products import MappedProductSource, compile_catalogue
from app.schemas.source_mapping import (
    DiscoveredColumn,
    DiscoveredObject,
    ProductMapping,
    SchemaDiscovery,
)
from app.services.source_mapping import connector_for
from sqlalchemy import Engine


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail("Readiness diagnostic attempted live I/O")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(subprocess, "run", deny)
    monkeypatch.setattr(subprocess, "Popen", deny)
    monkeypatch.setattr(Engine, "connect", deny)


@pytest.fixture
def source():
    # The actual mapper is exercised with arbitrary field names and product data.
    mapping = ProductMapping(
        capability="products",
        object_id="o1",
        key_columns=("item_key",),
        name_columns=("primary_label", "translation"),
        sku_column="item_key",
    )
    adapter = object.__new__(MappedProductSource)
    adapter.mapping = mapping
    return adapter


def row(**updates):
    record = {
        "key_0": uuid.uuid4().hex,
        "name_0": "Item " + uuid.uuid4().hex,
        "name_1": "",
        "sku": None,
    }
    record.update(updates)
    return record


@pytest.mark.parametrize("key", [None, "", " ", "\t\r\n", "\u00a0\u2003"])
def test_empty_keys_are_source_errors_without_discard_or_reconstruction(source, key):
    records = [row(), row(key_0=key), row()]
    before = copy.deepcopy(records)
    with pytest.raises(SourceMappingError, match="^catalogue_value_invalid$"):
        source._products(records)
    assert records == before


@pytest.mark.parametrize(
    "names", [(None, None), ("", ""), (" ", "\t"), ("\u00a0", "\u2003")]
)
def test_both_display_names_empty_are_rejected(source, names):
    with pytest.raises(SourceMappingError, match="^catalogue_value_invalid$"):
        source._products([row(name_0=names[0], name_1=names[1])])


@pytest.mark.parametrize(
    "names",
    [("Valid primary", ""), ("", "Valid translation"), (None, "Valid translation")],
)
def test_optional_empty_translation_or_primary_is_supported(source, names):
    result = source._products([row(name_0=names[0], name_1=names[1])])
    assert len(result) == 1
    assert result[0].names == tuple(n.strip() for n in names if n and n.strip())


@pytest.mark.parametrize("key", [0, 42, "000042", "0", "A-" + uuid.uuid4().hex])
def test_valid_numeric_or_text_keys_do_not_fail_conversion(source, key):
    assert len(source._products([row(key_0=key)])) == 1


def test_projection_keeps_text_columns_without_numeric_conversion(source):
    try:
        from sqlalchemy.dialects import mssql
    except OSError as exc:
        if exc.errno == 22:
            pytest.skip("Existing Windows SQL Server dialect file-read failure")
        raise

    discovery = SchemaDiscovery(
        engine="sqlserver",
        source_fingerprint="a" * 64,
        objects=(
            DiscoveredObject(
                object_id="o1",
                schema_name="shop",
                name="arbitrary_items",
                unique_keys=(("item_key",),),
                columns=tuple(
                    DiscoveredColumn(name=n, kind="text", nullable=False)
                    for n in ("item_key", "primary_label", "translation")
                ),
            ),
        ),
    )
    statement, parameters = compile_catalogue(source.mapping, discovery, None)
    sql = str(statement.compile(dialect=mssql.dialect()))
    assert "CAST(" not in sql.upper() and "CONVERT(" not in sql.upper()
    assert parameters == {}


def test_authoritative_source_repairs_resolve_errors_without_filtering(source):
    # Demonstrate the repair in synthetic data; no real source records are changed.
    original = [row(key_0="")] + [row(name_0="", name_1="") for _ in range(9)]
    unchanged = copy.deepcopy(original)
    corrected = copy.deepcopy(original)
    corrected[0]["key_0"] = uuid.uuid4().hex
    for record in corrected[1:]:
        record["name_0"] = "Verified synthetic item " + uuid.uuid4().hex
    assert len(source._products(corrected)) == len(original)
    assert original == unchanged
    with pytest.raises(SourceMappingError, match="catalogue_value_invalid"):
        source._products(original)


@pytest.mark.parametrize("mode", ["closed", "open"])
def test_current_admission_is_global_not_a_business_allowlist(mode):
    settings = Settings(_env_file=None, environment="testing", admission_mode=mode)
    for _business_id in (uuid.uuid4(), uuid.uuid4()):
        if mode == "closed":
            with pytest.raises(ApplicationError) as error:
                require_open_admission(settings)
            assert error.value.error_code == "admission_closed"
        else:
            assert require_open_admission(settings) is None


@pytest.mark.parametrize(
    "responsibility", ["summary", "customer", "knowledge", "outbound"]
)
def test_source_profile_restriction_alone_does_not_restrict_worker_admission(
    monkeypatch,
    responsibility,
):
    allowed_business, excluded_business = uuid.uuid4(), uuid.uuid4()
    registry = Mock()
    registry.get_profile.return_value = SimpleNamespace(
        business_ids=(allowed_business,),
        adapter_type="sqlserver_readonly",
        mapping_profile_key="discovered_products",
    )
    excluded_source = SimpleNamespace(
        business_id=excluded_business,
        connection_profile_key="synthetic_catalogue",
        adapter_type="sqlserver_readonly",
    )
    with pytest.raises(SourceMappingError, match="mapping_connection_not_authorized"):
        connector_for(registry, excluded_source)
    registry.connector.assert_not_called()
    settings = Settings(
        _env_file=None,
        environment="testing",
        admission_mode="open",
        owner_chat_provider="mock",
        source_mapping_provider="mock",
        conversation_summary_provider="mock",
        customer_chat_provider="mock",
    )
    monkeypatch.setattr(runtime, "get_current_job", lambda: None)
    # No claims, provider dispatch or database work: exercise the real contract gate.
    # It has no business/source context and therefore cannot enforce the proposed scope.
    runtime.validate_worker_job(settings, responsibility)
