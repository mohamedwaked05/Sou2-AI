"""Reusable catalogue contracts, dialect bounds and authenticated onboarding."""

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest
from app.agent.mapping_provider import (
    GeminiMappingProvider,
    MappingGeneration,
    get_mapping_provider,
)
from app.agent.owner_chat_provider import (
    GeminiOwnerChatProvider,
    OwnerChatProviderUnavailable,
    TokenUsage,
)
from app.core.config import get_settings
from app.core.exceptions import ApplicationError
from app.database.models import SourceMappingRevision, User
from app.integrations.discovery import (
    DiscoveryScope,
    EngineConnector,
    SourceConnection,
    SourceMappingError,
)
from app.integrations.mapped_products import (
    compile_catalogue,
    validate_mapping,
)
from app.integrations.profiles import ConnectionProfile, get_connection_profile_registry
from app.main import app
from app.schemas.source_mapping import (
    CatalogueRequest,
    CategoryMapping,
    DiscoveredColumn,
    DiscoveredObject,
    IdentifierMapping,
    JoinPair,
    MappingProposal,
    ProductMapping,
    SchemaDiscovery,
)
from app.tools.operational import OperationalToolExecutor, ToolExecutionError
from pydantic import SecretStr, ValidationError
from sqlalchemy import text
from sqlalchemy.dialects import mssql, postgresql
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from tests.test_data_sources import create_owner_business, headers


def opaque_discovery(engine="postgresql"):
    def column(name, kind="text", nullable=False):
        return DiscoveredColumn(name=name, kind=kind, nullable=nullable)

    return SchemaDiscovery(
        engine=engine,
        source_fingerprint="a" * 64,
        objects=(
            DiscoveredObject(
                object_id="o1",
                schema_name="scope",
                name="q9",
                columns=tuple(column(name) for name in ("s", "k", "a", "b", "c")),
                unique_keys=(("s", "k"),),
            ),
            DiscoveredObject(
                object_id="o2",
                schema_name="scope",
                name="r3",
                columns=tuple(column(name) for name in ("s", "c", "z")),
                unique_keys=(("s", "c"),),
            ),
            DiscoveredObject(
                object_id="o3",
                schema_name="scope",
                name="u8",
                columns=tuple(column(name) for name in ("s", "k", "v")),
                unique_keys=(("v",),),
            ),
        ),
    )


def opaque_mapping():
    return ProductMapping(
        object_id="o1",
        key_columns=("s", "k"),
        name_columns=("a", "b"),
        categories=(
            CategoryMapping(
                object_id="o2",
                label_column="z",
                joins=(
                    JoinPair(product_column="s", related_column="s"),
                    JoinPair(product_column="c", related_column="c"),
                ),
            ),
        ),
        identifiers=(
            IdentifierMapping(
                object_id="o3",
                value_column="v",
                kind="barcode",
                joins=(
                    JoinPair(product_column="s", related_column="s"),
                    JoinPair(product_column="k", related_column="k"),
                ),
            ),
        ),
    )


@pytest.mark.parametrize(
    "dialect,engine",
    [(postgresql.dialect(), "postgresql"), (mssql.dialect(), "sqlserver")],
)
def test_queries_quote_and_bind_opaque_composite_schema(dialect, engine):
    statement, values = compile_catalogue(
        opaque_mapping(),
        opaque_discovery(engine),
        CatalogueRequest(query="x%' OR 1=1 --[_", limit=7),
    )
    sql = str(statement.compile(dialect=dialect))
    assert "OR 1=1" not in sql
    assert "EXISTS" in sql and "LEFT OUTER JOIN" in sql
    assert values["term"] == "%x~%' OR 1=1 --~[~_%"
    assert "q9" in sql and "r3" in sql and "u8" in sql
    assert ("TOP" if engine == "sqlserver" else "LIMIT") in sql


@pytest.mark.parametrize(
    "change,code",
    [
        ({"object_id": "o9"}, "mapping_object_unknown"),
        ({"key_columns": ("k",)}, "mapping_product_key_not_unique"),
        ({"name_columns": ("secret",)}, "mapping_column_unknown"),
        ({"name_columns": ("a", "a")}, "mapping_duplicate_name"),
    ],
)
def test_malformed_mapping_rejected(change, code):
    with pytest.raises(SourceMappingError, match=code):
        validate_mapping(opaque_mapping().model_copy(update=change), opaque_discovery())


def test_partial_composite_join_and_sql_fragments_are_rejected():
    mapping = opaque_mapping()
    category = mapping.categories[0].model_copy(
        update={"joins": mapping.categories[0].joins[:1]}
    )
    with pytest.raises(SourceMappingError, match="mapping_category_join_not_unique"):
        validate_mapping(
            mapping.model_copy(update={"categories": (category,)}), opaque_discovery()
        )
    with pytest.raises(ValidationError):
        ProductMapping.model_validate(
            {**mapping.model_dump(), "sql": "SELECT * FROM accounts"}
        )
    with pytest.raises(ValidationError):
        ProductMapping.model_validate(
            {**mapping.model_dump(), "capability": "inventory"}
        )
    with pytest.raises(ValidationError):
        CatalogueRequest(query="x", limit=51)


class FixtureProvider:
    def __init__(self):
        self.calls = 0
        self.error = None
        self.usage = TokenUsage(120, 80, 200, True)

    def estimate_input_tokens(self, discovery):
        return 300

    def propose(self, discovery):
        self.calls += 1
        if self.error:
            raise self.error
        return MappingGeneration(
            proposal=MappingProposal(
                mapping=opaque_mapping(),
                uncertainties=("Reviewer must confirm names and category meaning.",),
                rationale=("Composite metadata mapping; semantics need review."),
            ),
            usage=self.usage,
            provider="mock",
            model="mapping-fixture",
        )


class FixtureRegistry:
    def __init__(self, connector, business_id):
        self.external = connector
        self.profile = ConnectionProfile(
            key="opaque_catalogue",
            display_name="Fixture catalogue",
            description="Read-only fixture",
            adapter_type=f"{connector.config.engine}_readonly",
            mapping_profile_key="discovered_products",
            mapping_profile_version=1,
            business_ids=(business_id,),
        )

    def available_profiles(self):
        return (self.profile,)

    def get_profile(self, key):
        return self.profile if key == self.profile.key else None

    def get_mapping(self, key, version):
        return None

    def connector(self, key, business_id):
        if key != self.profile.key or business_id not in self.profile.business_ids:
            raise SourceMappingError("not_authorized")
        return self.external


@pytest.fixture
def catalogue_flow(
    api_client, db_session, migration_engine, database_engine, monkeypatch
):
    user, business = create_owner_business(
        db_session, "mapping@example.com", "Opaque Test Catalogue"
    )
    schema = "mapping_fixture_" + uuid.uuid4().hex[:10]
    monkeypatch.setattr(
        get_settings(),
        "tool_call_audit_hmac_secret",
        SecretStr("mapping-fixture-audit-secret-32-characters"),
    )
    with migration_engine.begin() as connection:
        connection.execute(
            text("UPDATE public.businesses SET status='ACTIVE' WHERE id=:id"),
            {"id": business.id},
        )
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(
            text(f'''CREATE TABLE "{schema}".q9 (
              s text NOT NULL, k text NOT NULL, a text NOT NULL, b text NOT NULL,
              c text NOT NULL, private_contact text, PRIMARY KEY(s,k));
            CREATE TABLE "{schema}".r3 (
              s text NOT NULL,c text NOT NULL,z text NOT NULL,PRIMARY KEY(s,c));
            CREATE TABLE "{schema}".u8 (
              s text NOT NULL,k text NOT NULL,v text PRIMARY KEY);
            INSERT INTO "{schema}".q9(s,k,a,b,c) VALUES
              ('01','0007','Pepsi Mini','بيبسي صغير','04'),
              ('01','0008','Pepsi Diet','بيبسي دايت','04'),
              ('02','0007','Other','بديل','04');
            INSERT INTO "{schema}".r3 VALUES
              ('01','04','Drinks'),('02','04','Other category');
            INSERT INTO "{schema}".u8 VALUES ('01','0007','009999');
            GRANT USAGE ON SCHEMA "{schema}" TO sou2ai_runtime;
            GRANT SELECT(s,k,a,b,c) ON "{schema}".q9 TO sou2ai_runtime;
            GRANT SELECT ON "{schema}".r3,"{schema}".u8 TO sou2ai_runtime;
        ''')
        )
    db_session.expire_all()
    connector = EngineConnector(
        SourceConnection(
            key="opaque_catalogue",
            display_name="Fixture catalogue",
            engine="postgresql",
            url=database_engine.url.render_as_string(hide_password=False),
            business_ids=(business.id,),
            discovery_scope=(
                DiscoveryScope(
                    schema_name=schema,
                    object_name="q9",
                    columns=("s", "k", "a", "b", "c"),
                ),
                DiscoveryScope(
                    schema_name=schema, object_name="r3", columns=("s", "c", "z")
                ),
                DiscoveryScope(
                    schema_name=schema, object_name="u8", columns=("s", "k", "v")
                ),
            ),
        )
    )
    registry = FixtureRegistry(connector, business.id)
    provider = FixtureProvider()
    app.dependency_overrides[get_connection_profile_registry] = lambda: registry
    app.dependency_overrides[get_mapping_provider] = lambda: provider
    root = f"/api/v1/businesses/{business.id}/data-sources"
    response = api_client.post(
        root,
        headers=headers(user),
        json={
            "display_name": "Opaque catalogue",
            "connection_profile_key": "opaque_catalogue",
            "mapping_profile_key": "discovered_products",
            "mapping_profile_version": 1,
        },
    )
    assert response.status_code == 201, response.text
    yield (
        api_client,
        db_session,
        migration_engine,
        user,
        business,
        root + "/" + response.json()["id"],
        registry,
        provider,
    )
    connector.engine.dispose()
    with migration_engine.begin() as connection:
        connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def call(client, user, path, body=None):
    return client.post(path, headers=headers(user), json=body or {})


def prepare_approved(flow):
    client, session, migrator, user, business, path, registry, provider = flow
    response = call(
        client, user, path + "/mappings/propose", {"idempotency_key": str(uuid.uuid4())}
    )
    assert response.status_code == 200, response.text
    review = response.json()
    assert review["status"] == "review", review
    response = call(
        client,
        user,
        path + f"/mappings/{review['id']}/approve",
        {
            "mapping": review["proposal"]["mapping"],
            "confirm_semantics": True,
            "acknowledge_uncertainties": True,
        },
    )
    assert response.status_code == 200, response.text
    response = call(client, user, path + "/activate")
    assert response.status_code == 200, response.text
    assert response.json()["capabilities"] == ["products"]


def test_authenticated_mapping_flow_and_explicit_variant(catalogue_flow):
    client, session, migrator, user, business, path, registry, provider = catalogue_flow
    metadata = client.get(path + "/discovery", headers=headers(user))
    assert metadata.status_code == 200
    assert "private_contact" not in metadata.text
    assert "Pepsi" not in metadata.text
    assert call(client, user, path + "/activate").status_code == 409
    prepare_approved(catalogue_flow)
    english = call(client, user, path + "/products/search", {"query": "Pepsi"})
    arabic = call(client, user, path + "/products/search", {"query": "بيبسي"})
    assert english.status_code == arabic.status_code == 200
    assert english.json() == arabic.json()
    result = english.json()
    assert result["status"] == "ambiguous" and len(result["items"]) == 2
    assert all(item["stock"] is None for item in result["items"])
    chosen = call(
        client,
        user,
        path + "/products/search",
        {
            "external_product_id": result["items"][0]["external_product_id"],
            "mapping_version": result["mapping_version"],
        },
    )
    assert chosen.json()["status"] == "resolved"
    assert chosen.json()["items"][0]["categories"] == ["Drinks"]
    barcode = call(client, user, path + "/products/search", {"query": "009999"})
    assert barcode.json()["items"] == chosen.json()["items"]
    assert provider.calls == 1


def test_proposal_replay_has_one_call_and_no_outstanding_hold(catalogue_flow):
    client, session, migrator, user, business, path, registry, provider = catalogue_flow
    body = {"idempotency_key": str(uuid.uuid4())}
    first = call(client, user, path + "/mappings/propose", body)
    replay = call(client, user, path + "/mappings/propose", body)
    assert first.json() == replay.json()
    assert provider.calls == 1
    with migrator.connect() as connection:
        row = connection.execute(
            text(
                "SELECT total_tokens_used, tokens_reserved "
                "FROM public.business_ai_usage_daily WHERE business_id=:business"
            ),
            {"business": business.id},
        ).one()
        assert row.total_tokens_used == 200 and row.tokens_reserved == 0


def test_concurrent_replay_does_not_dispatch_or_reserve_twice(
    catalogue_flow, database_engine, monkeypatch
):
    from app.services.source_mapping import propose_mapping

    client, session, migrator, user, business, path, registry, provider = catalogue_flow
    user_id, business_id, source_id = (
        user.id,
        business.id,
        uuid.UUID(path.rsplit("/", 1)[1]),
    )
    key = uuid.uuid4()
    entered, release = Event(), Event()
    original = provider.propose

    def blocked(discovery):
        entered.set()
        assert release.wait(15)
        return original(discovery)

    monkeypatch.setattr(provider, "propose", blocked)

    def request():
        with Session(database_engine) as independent:
            return propose_mapping(
                independent,
                independent.get(User, user_id),
                business_id,
                source_id,
                key,
                registry,
                provider,
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(request)
        try:
            assert entered.wait(10)
            with pytest.raises(ApplicationError) as rejected:
                request()
            assert rejected.value.error_code == "mapping_proposal_in_progress"
            with migrator.connect() as connection:
                assert (
                    connection.execute(
                        text(
                            "SELECT tokens_reserved "
                            "FROM public.business_ai_usage_daily WHERE business_id=:id"
                        ),
                        {"id": business_id},
                    ).scalar_one()
                    == 2348
                )
        finally:
            release.set()
        assert running.result(timeout=10).status == "review"
    assert provider.calls == 1
    assert request().status == "review" and provider.calls == 1
    with migrator.connect() as connection:
        row = connection.execute(
            text(
                "SELECT total_tokens_used,tokens_reserved "
                "FROM public.business_ai_usage_daily WHERE business_id=:id"
            ),
            {"id": business_id},
        ).one()
        assert row.total_tokens_used == 200 and row.tokens_reserved == 0


def test_stale_explicit_selection_rejected_without_disabling_source(catalogue_flow):
    client, session, migrator, user, business, path, registry, provider = catalogue_flow
    prepare_approved(catalogue_flow)
    result = call(client, user, path + "/products/search", {"query": "Pepsi"}).json()
    selected = {"external_product_id": result["items"][0]["external_product_id"]}
    assert call(client, user, path + "/products/search", selected).status_code == 422
    stale = call(
        client,
        user,
        path + "/products/search",
        {
            **selected,
            "mapping_version": result["mapping_version"] + 1,
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "mapping_selection_stale"
    assert client.get(path, headers=headers(user)).json()["status"] == "ACTIVE"


def test_committed_admission_precedes_dispatch_and_budget_rejection(
    catalogue_flow, monkeypatch
):
    client, session, migrator, user, business, path, registry, provider = catalogue_flow
    original = provider.propose

    def inspect_admission(discovery):
        with migrator.connect() as connection:
            held = connection.execute(
                text(
                    "SELECT tokens_reserved FROM public.business_ai_usage_daily "
                    "WHERE business_id=:id"
                ),
                {"id": business.id},
            ).scalar_one()
            assert held == 2348
        return original(discovery)

    monkeypatch.setattr(provider, "propose", inspect_admission)
    first = call(
        client,
        user,
        path + "/mappings/propose",
        {
            "idempotency_key": str(uuid.uuid4()),
        },
    )
    assert first.json()["status"] == "review"
    monkeypatch.setattr(provider, "estimate_input_tokens", lambda discovery: 20_000)
    rejected = call(
        client,
        user,
        path + "/mappings/propose",
        {
            "idempotency_key": str(uuid.uuid4()),
        },
    )
    assert rejected.status_code == 429 and provider.calls == 1
    with migrator.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT tokens_reserved FROM public.business_ai_usage_daily "
                    "WHERE business_id=:id"
                ),
                {"id": business.id},
            ).scalar_one()
            == 0
        )


def test_product_only_dispatch_and_immutable_approval(catalogue_flow):
    client, session, migrator, user, business, path, registry, provider = catalogue_flow
    prepare_approved(catalogue_flow)
    executor = OperationalToolExecutor(session, registry, get_settings())
    with pytest.raises(ToolExecutionError) as denied:
        executor.execute(
            user=user,
            business_id=business.id,
            tool_name="current_inventory",
            arguments={"product_filter": "Pepsi"},
        )
    assert denied.value.code == "capability_unavailable"
    revision = (
        session.query(SourceMappingRevision).filter_by(business_id=business.id).one()
    )
    with pytest.raises(DBAPIError):
        session.execute(
            text(
                "UPDATE public.source_mapping_revisions "
                "SET approved_mapping='{}'::jsonb WHERE id=:id"
            ),
            {"id": revision.id},
        )
    session.rollback()
    with pytest.raises(DBAPIError):
        session.execute(
            text("UPDATE public.ai_usage_reservations SET reserved_tokens=0")
        )
    session.rollback()


def test_query_bounds_and_string_composite_keys(catalogue_flow):
    client, session, migrator, user, business, path, registry, provider = catalogue_flow
    prepare_approved(catalogue_flow)
    limited = call(
        client, user, path + "/products/search", {"query": "Pepsi", "limit": 1}
    )
    assert limited.json()["truncated"] and limited.json()["status"] == "ambiguous"
    assert len(limited.json()["items"]) == 1
    from app.integrations.mapped_products import decode_product_key

    assert decode_product_key(limited.json()["items"][0]["external_product_id"], 2) == (
        "01",
        "0007",
    )
    literal = call(client, user, path + "/products/search", {"query": "%' OR 1=1 --"})
    assert literal.status_code == 200 and literal.json()["status"] == "not_found"


def test_provider_failure_unknown_usage_is_charged_once(catalogue_flow):
    client, session, migrator, user, business, path, registry, provider = catalogue_flow
    provider.error = OwnerChatProviderUnavailable(reason="quota", usage_uncertain=True)
    body = {"idempotency_key": str(uuid.uuid4())}
    response = call(client, user, path + "/mappings/propose", body)
    assert response.json()["status"] == "failed"
    assert (
        call(client, user, path + "/mappings/propose", body).json() == response.json()
    )
    with migrator.connect() as connection:
        row = connection.execute(
            text(
                "SELECT total_tokens_used, tokens_reserved "
                "FROM public.business_ai_usage_daily WHERE business_id=:business"
            ),
            {"business": business.id},
        ).one()
        assert row.total_tokens_used == 2348 and row.tokens_reserved == 0
    assert provider.calls == 1


def test_schema_drift_gates_source_and_permission_rejection(catalogue_flow):
    client, session, migrator, user, business, path, registry, provider = catalogue_flow
    prepare_approved(catalogue_flow)
    schema = registry.external.config.discovery_scope[0].schema_name
    with migrator.begin() as connection:
        connection.execute(
            text(f'ALTER TABLE "{schema}".q9 ALTER COLUMN b DROP NOT NULL')
        )
    response = call(client, user, path + "/products/search", {"query": "Pepsi"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "mapping_schema_changed"
    assert client.get(path, headers=headers(user)).json()["status"] == "UNHEALTHY"
    with migrator.begin() as connection:
        connection.execute(
            text(f'REVOKE SELECT(b) ON "{schema}".q9 FROM sou2ai_runtime')
        )
    assert client.get(path + "/discovery", headers=headers(user)).status_code == 422


def test_cross_tenant_profiles_and_reviews_are_hidden(catalogue_flow):
    client, session, migrator, user, business, path, registry, provider = catalogue_flow
    other, other_business = create_owner_business(
        session, "other-mapping@example.com", "Other Catalogue"
    )
    assert client.get(path + "/discovery", headers=headers(other)).status_code == 404
    assert (
        client.get(
            f"/api/v1/businesses/{other_business.id}/data-sources/available-profiles",
            headers=headers(other),
        ).json()
        == []
    )
    assert provider.calls == 0


def test_gemini_contract_handles_untrusted_metadata_without_records():
    discovery = opaque_discovery()
    seen = []

    def respond(request):
        payload = json.loads(request.content)
        seen.append(payload)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {
                                    "text": MappingProposal(
                                        mapping=opaque_mapping(),
                                        uncertainties=("Confirm semantics.",),
                                        rationale="Composite metadata mapping.",
                                    ).model_dump_json()
                                }
                            ]
                        },
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 80,
                    "totalTokenCount": 180,
                },
            },
        )

    provider = GeminiMappingProvider(
        GeminiOwnerChatProvider(
            api_key="test-not-a-secret",
            model="gemini-3-flash-preview",
            timeout_seconds=2,
            transport=httpx.MockTransport(respond),
        )
    )
    result = provider.propose(discovery)
    assert result.proposal.mapping == opaque_mapping()
    assert result.usage.total_tokens == 180
    serialized = json.dumps(seen)
    assert "source_fingerprint" not in serialized and "Pepsi" not in serialized
    assert "SELECT" not in serialized
    assert seen[0]["generationConfig"]["maxOutputTokens"] == 2048


def test_sqlserver_opaque_schema_application_flow(catalogue_flow):
    supplied = os.getenv("MAPPING_SQLSERVER_FIXTURE_URL")
    if supplied is None:
        pytest.skip("Requires isolated SQL Server fixture credentials.")
    client, session, migrator, user, business, old_path, old_registry, provider = (
        catalogue_flow
    )
    connector = EngineConnector(
        SourceConnection(
            key="opaque_catalogue",
            display_name="Different SQL catalogue",
            engine="sqlserver",
            url=supplied,
            business_ids=(business.id,),
            discovery_scope=(
                DiscoveryScope(
                    schema_name="scope_4",
                    object_name="n17",
                    columns=("s", "k", "a", "b", "c"),
                ),
                DiscoveryScope(
                    schema_name="scope_4", object_name="n28", columns=("s", "c", "z")
                ),
                DiscoveryScope(
                    schema_name="scope_4", object_name="n39", columns=("s", "k", "v")
                ),
            ),
        )
    )
    registry = FixtureRegistry(connector, business.id)
    app.dependency_overrides[get_connection_profile_registry] = lambda: registry
    root = old_path.rsplit("/", 1)[0]
    response = client.post(
        root,
        headers=headers(user),
        json={
            "display_name": "SQL opaque catalogue",
            "connection_profile_key": "opaque_catalogue",
            "mapping_profile_key": "discovered_products",
            "mapping_profile_version": 1,
        },
    )
    assert response.status_code == 201
    path = root + "/" + response.json()["id"]
    flow = (client, session, migrator, user, business, path, registry, provider)
    prepare_approved(flow)
    metadata = client.get(path + "/discovery", headers=headers(user)).json()
    assert len(metadata["relationships"]) == 1
    assert "private_contact" not in json.dumps(metadata)
    result = call(client, user, path + "/products/search", {"query": "Pepsi"})
    assert result.status_code == 200
    assert result.json()["status"] == "ambiguous" and len(result.json()["items"]) == 2
    arabic = call(client, user, path + "/products/search", {"query": "بيبسي"})
    assert arabic.status_code == 200 and len(arabic.json()["items"]) == 2
    selected = call(
        client,
        user,
        path + "/products/search",
        {
            "external_product_id": result.json()["items"][0]["external_product_id"],
            "mapping_version": result.json()["mapping_version"],
        },
    )
    assert selected.json()["status"] == "resolved"
    assert selected.json()["items"][0]["categories"] == ["Drinks"]
    assert selected.json()["items"][0]["stock"] is None
    bad_config = connector.config.model_copy(
        update={
            "discovery_scope": (
                DiscoveryScope(
                    schema_name="scope_4",
                    object_name="n17",
                    columns=("k", "private_contact"),
                ),
            )
        }
    )
    rejected = EngineConnector(bad_config)
    with pytest.raises(SourceMappingError, match="discovery_permission"):
        rejected.discover()
    rejected.engine.dispose()
    connector.engine.dispose()
