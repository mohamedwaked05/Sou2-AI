"""Real PostgreSQL capability reports, independent approval and execution."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.database.models import OperationalDataSourceConfig, SourceMappingRevision
from app.integrations.discovery import EngineConnector, SourceConnection
from app.integrations.profiles import get_connection_profile_registry
from app.main import app
from pydantic import SecretStr
from sqlalchemy import text
from tests.operational_isolated.test_native_operations import (
    native_source as native_source,
)
from tests.test_data_sources import headers


@pytest.fixture(scope="session")
def operations_platform(quality_platform):
    return quality_platform


@pytest.fixture
def system(quality_platform, native_source, tenant):
    fixture, _ = native_source
    user, business_id, client = tenant
    source_id = uuid.uuid4()
    connector = EngineConnector(
        SourceConnection(
            key="capability_fixture",
            display_name="Synthetic capability source",
            engine="postgresql",
            url=SecretStr(
                quality_platform["admin_url"].replace(
                    "reservation_test_admin@", "isolated_source_reader@"
                )
            ),
            business_ids=(business_id,),
            discovery_scope=tuple(
                dict(
                    schema_name="public",
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

    product = translate(fixture.catalogue.mapping.model_dump(mode="json"))
    mapping = translate(fixture.mapping.model_dump(mode="json"))
    mapping.update(
        business_id=str(business_id),
        source_id=str(source_id),
        source_fingerprint=discovery.source_fingerprint,
        schema_fingerprint=discovery.schema_fingerprint,
    )
    registry = SimpleNamespace(
        get_profile=lambda _: SimpleNamespace(
            business_ids=(business_id,),
            adapter_type="postgresql_readonly",
            mapping_profile_key="discovered_products",
        ),
        connector=lambda *_: connector,
    )
    app.dependency_overrides[get_connection_profile_registry] = lambda: registry
    with quality_platform["factory"]() as session:
        source = OperationalDataSourceConfig(
            id=source_id,
            business_id=business_id,
            display_name="Capability test",
            adapter_type="postgresql_readonly",
            connection_profile_key="capability_fixture",
            mapping_profile_key="discovered_products",
            mapping_profile_version=1,
            status="UNHEALTHY",
            failure_code="catalogue_value_invalid",
            last_validated_at=datetime.now(UTC),
        )
        session.add(source)
        session.flush()
        revision = SourceMappingRevision(
            id=uuid.uuid4(),
            business_id=business_id,
            source_id=source_id,
            requested_by=user.id,
            idempotency_key=uuid.uuid4(),
            version=1,
            status="proposing",
            discovery=discovery.model_dump(mode="json"),
            schema_fingerprint=discovery.schema_fingerprint,
        )
        session.add(revision)
        session.flush()
        revision.status = "review"
        session.flush()
        revision.status = "approved"
        revision.approved_mapping = product
        revision.approved_by = user.id
        revision.approved_at = datetime.now(UTC)
        session.commit()
    with fixture.engine.begin() as connection:
        connection.execute(
            fixture.tables[0]
            .update()
            .where(fixture.tables[0].c.tenant == "A")
            .values(label="")
        )
    yield SimpleNamespace(
        client=client,
        auth=headers(user),
        fixture=fixture,
        registry=registry,
        business_id=business_id,
        source_id=source_id,
        user=user,
        platform=quality_platform,
        url=f"/api/v1/businesses/{business_id}/data-sources/{source_id}",
        body=dict(
            mapping=mapping,
            discovery=discovery.model_dump(mode="json"),
            meaning=(
                "Explicit synthetic reviewed semantics; all completed sales "
                "and refunds are represented."
            ),
        ),
    )
    with fixture.engine.begin() as connection:
        connection.execute(
            fixture.tables[0]
            .update()
            .where(fixture.tables[0].c.tenant == "A")
            .values(label="Random Diet 7UP")
        )
    app.dependency_overrides.pop(get_connection_profile_registry, None)
    connector.engine.dispose()


def validate(system, family):
    response = system.client.post(
        system.url + f"/capabilities/{family}/validate",
        headers=system.auth,
        json=system.body,
    )
    assert response.status_code == 200, response.text
    return response.json()


def approve(system, family, report):
    return system.client.post(
        system.url + f"/capabilities/{family}/approve",
        headers=system.auth,
        json=dict(
            report_id=report["id"],
            integrity=report["integrity"],
            total_records=report["total_records"],
            excluded_records=0,
            confirm_semantics=True,
            confirm_scope=True,
            confirm_enable_capability=True,
        ),
    )


@pytest.mark.parametrize(
    "family,operation",
    [
        ("inventory", "stock"),
        ("pricing", "prices"),
        ("sales", "sales"),
        ("financial", "revenue"),
    ],
)
def test_independent_native_approval_and_execution_despite_nameless_catalogue(
    system, family, operation
):
    report = validate(system, family)
    assert report["eligible"] and report["invalid_records"] == 0
    assert report["impact"] == "SAFE_FOR_CAPABILITY"
    approved = approve(system, family, report)
    assert approved.status_code == 200, approved.text
    replay = approve(system, family, report)
    assert replay.json()["approval_id"] == approved.json()["approval_id"]
    request = dict(
        capability=operation,
        catalogue_revision=1,
        mapping_revision=approved.json()["version"],
        start="2026-10-14T00:00:00Z",
        end="2026-10-17T00:00:00Z",
    )
    if operation == "prices":
        # Use the approved original composite product key, not a fabricated ID.
        from app.integrations.mapped_products import encode_product_key

        request.update(
            product_id=encode_product_key(("A", "001")), as_of="2026-10-15T12:00:00Z"
        )
    response = system.client.post(
        system.url + "/operations/read", headers=system.auth, json=request
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok", response.text
    assert (
        system.client.get(system.url, headers=system.auth).json()["status"]
        == "UNHEALTHY"
    )


def test_revoking_inventory_keeps_financial_active(system):
    stock = approve(system, "inventory", validate(system, "inventory"))
    finance = approve(system, "financial", validate(system, "financial"))
    assert stock.status_code == finance.status_code == 200
    assert (
        system.client.post(
            system.url + "/capabilities/inventory/revoke", headers=system.auth
        ).status_code
        == 204
    )
    states = system.client.get(system.url + "/capabilities", headers=system.auth)
    assert states.status_code == 200, states.text
    states = {s["capability"]: s for s in states.json()["capabilities"]}
    assert states["inventory"]["approval_status"] == "REVOKED"
    assert states["financial"]["status"] == "ACTIVE"


def test_reviewed_profile_deadline_reaches_the_controlled_tool(system):
    from app.core.config import Settings
    from app.tools.operational import OperationalToolExecutor

    connector = system.registry.connector(None)
    connector.config = SourceConnection.model_validate(
        {**connector.config.model_dump(), "query_timeout_seconds": 5}
    )
    report = validate(system, "sales")
    assert approve(system, "sales", report).status_code == 200
    settings = Settings(
        _env_file=None,
        environment="testing",
        operational_query_timeout_seconds=2,
        tool_call_audit_hmac_secret=SecretStr("isolated-profile-deadline-audit-secret"),
    )
    with system.platform["factory"]() as session:
        executor = OperationalToolExecutor(session, system.registry, settings)
        definitions = executor.available_definitions(system.user, system.business_id)
        report_tool = next(d for d in definitions if d.name == "operational_report")
        assert report_tool.timeout_seconds == 5
        assert settings.operational_query_timeout_seconds == 2
        result = executor.execute(
            user=system.user,
            business_id=system.business_id,
            tool_name="operational_report",
            arguments={
                "capability": "sales",
                "start": "2026-10-14T00:00:00Z",
                "end": "2026-10-17T00:00:00Z",
            },
        )
        assert result.output.status == "ok"
        assert result.output.business_meaning == report["meaning"]


def test_null_financial_amount_blocks_without_dropping_transaction(system):
    table = system.fixture.tables[3]
    with system.fixture.engine.begin() as connection:
        connection.execute(table.update().where(table.c.tenant == "A").values(net=None))
    try:
        report = validate(system, "financial")
        assert not report["eligible"] and report["invalid_records"] == 1
        assert report["reason_counts"]["invalid_amount"] == 1
        assert approve(system, "financial", report).status_code == 409
        assert validate(system, "inventory")["eligible"]
    finally:
        with system.fixture.engine.begin() as connection:
            connection.execute(
                table.update().where(table.c.tenant == "A").values(net=18)
            )


def test_nullable_enforced_key_requires_exact_valid_values(system):
    table = system.fixture.tables[1]
    quote = system.fixture.engine.dialect.identifier_preparer.quote
    name = quote(table.name)
    constraint = quote(table.name + "_nullable_quality_key")
    columns = [table.c[n].name for n in ("tenant", "product", "location", "unit")]
    with system.fixture.engine.begin() as connection:
        connection.execute(
            text(
                f"ALTER TABLE public.{name} ADD CONSTRAINT {constraint} "
                f"UNIQUE ({','.join(quote(c) for c in columns)})"
            )
        )
    try:
        connector = system.registry.connector(None)
        discovery = connector.discover()
        system.body["discovery"] = discovery.model_dump(mode="json")
        system.body["mapping"]["schema_fingerprint"] = discovery.schema_fingerprint
        system.body["mapping"]["stock"]["key_columns"] = columns
        assert validate(system, "inventory")["eligible"]
        with system.fixture.engine.begin() as connection:
            connection.execute(
                table.update().where(table.c.tenant == "A").values(location=None)
            )
        report = validate(system, "inventory")
        assert report["invalid_records"] == 1 and not report["eligible"]
        assert report["impact"] == "BLOCKED_BY_DATA_QUALITY"
        assert approve(system, "inventory", report).status_code == 409
    finally:
        with system.fixture.engine.begin() as connection:
            connection.execute(
                table.update().where(table.c.tenant == "A").values(location="west")
            )
            connection.execute(
                text(f"ALTER TABLE public.{name} DROP CONSTRAINT {constraint}")
            )


def test_changed_fact_data_rejects_old_approval_but_other_family_approves(system):
    report = validate(system, "inventory")
    table = system.fixture.tables[1]
    with system.fixture.engine.begin() as connection:
        connection.execute(
            table.update().where(table.c.tenant == "A").values(balance=99)
        )
    try:
        assert approve(system, "inventory", report).status_code == 409
        assert (
            approve(system, "financial", validate(system, "financial")).status_code
            == 200
        )
    finally:
        with system.fixture.engine.begin() as connection:
            connection.execute(
                table.update().where(table.c.tenant == "A").values(balance=-2.125)
            )


def test_header_financial_totals_need_no_catalogue_rows_or_fake_units(system):
    mapping = system.body["mapping"]
    sales = mapping["sales"]
    sales.update(
        grain="transaction_events",
        product_reference="none",
        product_columns=[],
        unit=None,
        quantity=None,
        transaction_identity="enforced_header_key",
        transaction_columns=[
            {"object_id": sales["object_id"], "column": c} for c in sales["key_columns"]
        ],
        returns_semantics="no_returns",
    )
    mapping["refunds"] = None
    report = validate(system, "financial")
    assert report["eligible"] and report["invalid_records"] == 0
    approved = approve(system, "financial", report)
    assert approved.status_code == 200, approved.text
    response = system.client.post(
        system.url + "/operations/read",
        headers=system.auth,
        json={
            "capability": "revenue",
            "catalogue_revision": report["mapping"]["catalogue_revision"],
            "mapping_revision": approved.json()["version"],
            "start": "2026-10-14T00:00:00Z",
            "end": "2026-10-17T00:00:00Z",
        },
    )
    assert response.status_code == 200, response.text
    output = response.json()
    assert output["rows"][0]["product_id"] is None
    assert output["rows"][0]["unit"] is None
    assert output["rows"][0]["gross_quantity"] is None
    assert output["totals"][0]["transaction_count"] == 1
    from decimal import Decimal

    assert Decimal(output["totals"][0]["gross_revenue"]) == 20


def test_discount_formula_preserves_financial_definition(system):
    mapping = system.body["mapping"]
    sales = system.fixture.tables[3]
    mapping["revenue"].update(
        net_rule="gross_minus_discount",
        net_amount=None,
        discount_amount={
            "object_id": mapping["sales"]["object_id"],
            "column": sales.c.net.name,
        },
    )
    # Reviewed discount(18) is at this fact grain: gross(20)-discount(18)-refund(9)=-7.
    report = validate(system, "financial")
    assert report["eligible"]
    approved = approve(system, "financial", report)
    response = system.client.post(
        system.url + "/operations/read",
        headers=system.auth,
        json={
            "capability": "revenue",
            "catalogue_revision": 1,
            "mapping_revision": approved.json()["version"],
            "start": "2026-10-14T00:00:00Z",
            "end": "2026-10-17T00:00:00Z",
        },
    )
    assert response.status_code == 200, response.text
    from decimal import Decimal

    money = next(t for t in response.json()["totals"] if t["currency"] == "USD")
    assert Decimal(money["net_revenue"]) == -7


def test_original_ids_route_to_approved_inventory_without_catalogue_names(system):
    from app.core.config import get_settings
    from app.tools.operational import OperationalToolExecutor

    approved = approve(system, "inventory", validate(system, "inventory"))
    assert approved.status_code == 200, approved.text
    with system.platform["factory"]() as session:
        executor = OperationalToolExecutor(session, system.registry, get_settings())
        definitions = executor.available_definitions(system.user, system.business_id)
        assert len(definitions) == 1 and definitions[0].name == "operational_report"
        assert set(
            definitions[0].provider_schema()["input_schema"]["properties"][
                "capability"
            ]["enum"]
        ) == {"stock", "availability", "restocking"}
        result = executor.execute(
            user=system.user,
            business_id=system.business_id,
            tool_name="operational_report",
            arguments={"capability": "stock", "product_identity": ["A", "001"]},
        )
        assert result.output.status == "ok" and result.output.rows[0].on_hand < 0


def test_cross_business_cannot_read_or_approve_capability_evidence(system):
    report = validate(system, "inventory")
    wrong = system.url.replace(str(system.business_id), str(uuid.uuid4()))
    assert (
        system.client.get(
            wrong + "/capabilities/inventory/quality", headers=system.auth
        ).status_code
        == 404
    )

    assert (
        system.client.post(
            wrong + "/capabilities/inventory/approve",
            headers=system.auth,
            json={
                "report_id": report["id"],
                "integrity": report["integrity"],
                "total_records": report["total_records"],
                "confirm_semantics": True,
                "confirm_scope": True,
                "confirm_enable_capability": True,
            },
        ).status_code
        == 404
    )


def test_inventory_fact_identity_needs_no_catalogue_approval(system):
    source_id = uuid.uuid4()
    with system.platform["factory"]() as session:
        session.add(
            OperationalDataSourceConfig(
                id=source_id,
                business_id=system.business_id,
                display_name="Independent balances",
                adapter_type="postgresql_readonly",
                connection_profile_key="capability_fixture",
                mapping_profile_key="discovered_products",
                mapping_profile_version=1,
                status="CONFIGURED",
            )
        )
        session.commit()
    system.source_id = source_id
    system.url = f"/api/v1/businesses/{system.business_id}/data-sources/{source_id}"
    system.body["mapping"].update(source_id=str(source_id), catalogue_revision=0)
    system.body["mapping"]["stock"]["product_reference"] = "fact_identity"
    report = validate(system, "inventory")
    assert report["revision_id"] is None and report["identity_mapping"] is None
    assert report["eligible"]
    approved = approve(system, "inventory", report)
    assert approved.status_code == 200, approved.text
    response = system.client.post(
        system.url + "/operations/read",
        headers=system.auth,
        json={
            "capability": "stock",
            "catalogue_revision": 0,
            "mapping_revision": approved.json()["version"],
        },
    )
    assert response.status_code == 200 and response.json()["rows"]


def test_legacy_http_checkbox_cannot_bypass_capability_evidence(system, monkeypatch):
    def unexpected_discovery(*args, **kwargs):
        pytest.fail(
            "A legacy checkbox must not dispatch validation or approve new capabilities"
        )

    monkeypatch.setattr(
        system.registry.connector(None, None), "discover", unexpected_discovery
    )
    response = system.client.post(
        system.url + "/operational-mappings/approve",
        headers=system.auth,
        json={
            "mapping": system.body["mapping"],
            "discovery": system.body["discovery"],
            "confirm_semantics": True,
            "confirm_row_isolation": True,
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "capability_quality_review_required"


def test_native_business_dates_require_complete_local_day_bounds(system):
    table = system.fixture.tables[3]
    quote = system.fixture.engine.dialect.identifier_preparer.quote_identifier
    qualified = f"public.{quote(table.name)}"
    column = quote(table.c.at.name)
    with system.fixture.engine.begin() as connection:
        connection.execute(
            text(
                f"ALTER TABLE {qualified} ALTER COLUMN {column} "
                f"TYPE date USING {column}::date"
            )
        )
    try:
        discovery = system.registry.connector(None, None).discover()
        system.body["discovery"] = discovery.model_dump(mode="json")
        system.body["mapping"]["sales"]["timestamp_basis"] = "business_date"
        report = validate(system, "financial")
        assert report["eligible"], report
        approved = approve(system, "financial", report)
        assert approved.status_code == 200, approved.text
        request = {
            "capability": "revenue",
            "catalogue_revision": 1,
            "mapping_revision": approved.json()["version"],
            "start": "2026-10-14T21:00:00Z",
            "end": "2026-10-15T21:00:00Z",
        }
        response = system.client.post(
            system.url + "/operations/read", headers=system.auth, json=request
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ok"
        request["start"] = "2026-10-14T22:00:00Z"
        assert (
            system.client.post(
                system.url + "/operations/read", headers=system.auth, json=request
            ).status_code
            == 409
        )
    finally:
        with system.fixture.engine.begin() as connection:
            connection.execute(
                text(
                    f"ALTER TABLE {qualified} ALTER COLUMN {column} "
                    f"TYPE timestamptz USING {column}::timestamptz"
                )
            )
            connection.execute(
                table.update().values(at=datetime(2026, 10, 15, 12, tzinfo=UTC))
            )


def test_catalogue_name_permission_loss_does_not_block_identity_inventory(system):
    table = system.fixture.tables[0]
    quote = system.fixture.engine.dialect.identifier_preparer.quote_identifier
    qualified = f"public.{quote(table.name)}"
    columns = ",".join(quote(c.name) for c in table.c if c.key != "label")
    with system.fixture.engine.begin() as connection:
        connection.execute(
            text(f"REVOKE SELECT ON {qualified} FROM isolated_source_reader")
        )
        connection.execute(
            text(f"GRANT SELECT({columns}) ON {qualified} TO isolated_source_reader")
        )
    try:
        report = validate(system, "inventory")
        assert report["eligible"]
        product = report["discovery"]["objects"][0]
        assert all(c["name"] != table.c.label.name for c in product["columns"])
        assert approve(system, "inventory", report).status_code == 200
        workspace_response = system.client.get(
            system.url + "/capabilities", headers=system.auth
        )
        assert workspace_response.status_code == 200, workspace_response.text
        assert (
            workspace_response.json()["connection_status"]
            == "CONNECTED_READ_SCOPE_LIMITED"
        )
        assert (
            next(
                c
                for c in workspace_response.json()["capabilities"]
                if c["capability"] == "inventory"
            )["status"]
            == "ACTIVE"
        )
    finally:
        with system.fixture.engine.begin() as connection:
            connection.execute(
                text(f"GRANT SELECT ON {qualified} TO isolated_source_reader")
            )


def test_unrelated_catalogue_names_do_not_stale_inventory_evidence(system):
    report = validate(system, "inventory")
    product = system.fixture.tables[0]
    with system.fixture.engine.begin() as connection:
        connection.execute(
            product.update()
            .where(product.c.tenant == "A")
            .values(label="Changed unrelated display label")
        )
    assert approve(system, "inventory", report).status_code == 200


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("currency", "US$", "invalid_currency"),
        ("state", "unreviewed", "unclassified_status"),
        ("at", None, "invalid_event"),
    ],
)
def test_financial_relevant_invalid_values_block_only_financial(
    system, field, value, reason
):
    table = system.fixture.tables[3]
    original = {
        "currency": "USD",
        "state": "completed",
        "at": datetime(2026, 10, 15, 12, tzinfo=UTC),
    }
    with system.fixture.engine.begin() as connection:
        connection.execute(
            table.update().where(table.c.tenant == "A").values({table.c[field]: value})
        )
    try:
        report = validate(system, "financial")
        assert report["invalid_records"] == 1 and report["reason_counts"][reason] == 1
        assert (
            not report["eligible"]
            and approve(system, "financial", report).status_code == 409
        )
        assert validate(system, "inventory")["eligible"]
    finally:
        with system.fixture.engine.begin() as connection:
            connection.execute(
                table.update()
                .where(table.c.tenant == "A")
                .values({table.c[field]: original[field]})
            )


@pytest.mark.parametrize(
    "message",
    [
        "How much inventory is in stock for A/001?",
        "كم الكمية في المخزون للمنتج A/001؟",
        "adde 3anna stock lal A/001?",
    ],
)
def test_owner_chat_dispatches_only_independently_approved_tool(system, message):
    from app.agent.owner_chat_provider import (
        OwnerChatResult,
        TokenUsage,
        get_owner_chat_provider,
    )
    from tests.test_owner_chat import CapturingProvider, submit

    approved = approve(system, "inventory", validate(system, "inventory"))
    assert approved.status_code == 200, approved.text
    provider = CapturingProvider(
        OwnerChatResult(
            reply="",
            decision="tool",
            semantic_operation="operational_report",
            tool_name="operational_report",
            tool_arguments={"capability": "stock", "product_identity": ["A", "001"]},
            usage=TokenUsage(20, 20, 40, authoritative=True),
        )
    )
    app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    try:
        response = submit(
            system.client, system.user, system.business_id, message, uuid.uuid4().hex
        )
        assert response.status_code == 200, response.text
        assert "on_hand" in response.json()["assistant_message"]["content"], (
            response.text
        )
        assert len(provider.requests) == 1
        tools = provider.requests[0].tools
        assert [tool.name for tool in tools] == ["operational_report"]
    finally:
        app.dependency_overrides.pop(get_owner_chat_provider, None)
