"""Regression coverage for Owner AI routing and validated live-data answers."""

import json
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from app.agent.owner_chat_provider import (
    GeminiOwnerChatProvider,
    OwnerChatProviderInvalidResponse,
    OwnerChatProviderUnavailable,
    OwnerChatResult,
    get_owner_chat_provider,
)
from app.core.config import Settings, get_settings
from app.database.models import (
    OperationalDataSourceConfig,
    OwnerChatMessage,
    PendingOwnerOperationalPreference,
    UserOperationalPreference,
)
from app.database.session import get_engine, get_session_factory
from app.integrations.profiles import (
    FAKE_STORE_PROFILE,
    EnvironmentConnectionProfileRegistry,
    get_connection_profile_registry,
)
from app.main import app, create_app
from app.schemas.operational import (
    InventoryResult,
    ProductResolution,
    ProductResolutionCandidate,
    ProductResolutionQuery,
    SalesQuery,
)
from app.services import owner_chat
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select, text

from tests.test_api_security import production_settings
from tests.test_operational_postgresql import operational_adapter  # noqa: F401
from tests.test_operational_tools import (
    CURRENT_INVENTORY_TOOL,
    SALES_SUMMARY_TOOL,
    FailingSecondProvider,
    SequenceProvider,
    StubRegistry,
    StubSource,
    audit_settings,
    configure_operational_chat,
    inventory_item,
    metadata,
    usage_result,
)
from tests.test_owner_chat import active_business, submit
from tests.test_owner_chat_provider import operational_request


@pytest.mark.parametrize("environment", ["testing", "staging", "production"])
@pytest.mark.parametrize(
    "field",
    ["development_owner_chat_minute_limit", "development_owner_chat_hour_limit"],
)
def test_generation_limit_override_is_explicitly_development_only(environment, field):
    values = {field: 30 if "minute" in field else 300}
    with pytest.raises(ValidationError, match="ENVIRONMENT=development"):
        if environment == "production":
            production_settings(**values)
        else:
            Settings(_env_file=None, environment=environment, **values)


@pytest.mark.parametrize(
    "environment,limit", [("testing", 3), ("development", 6), ("production", 3)]
)
def test_generation_limit_default_and_development_override(
    api_client, db_session, migration_engine, environment, limit
):
    user, business = active_business(api_client, db_session)
    settings = get_settings().model_copy(
        update={
            "environment": environment,
            # Test the runtime environment guard as well as settings validation.
            "development_owner_chat_minute_limit": 6,
            "development_owner_chat_hour_limit": 30,
        }
    )
    provider = SequenceProvider([OwnerChatResult(reply="Hello!")] * limit)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    for index in range(limit):
        response = submit(api_client, user, business["id"], "kifak", f"allowed-{index}")
        assert response.status_code == 200, response.text
    blocked = submit(api_client, user, business["id"], "kifak", "blocked")
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "owner_chat_rate_limited"
    assert "Retry-After" in blocked.headers
    assert len(provider.requests) == limit
    with migration_engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT count(*) FROM owner_chat_rate_limit_events"))
            == limit
        )


def test_failed_provider_requests_count_but_terminal_replays_do_not(
    api_client, db_session, migration_engine
):
    user, business = active_business(api_client, db_session)

    class UnavailableProvider(SequenceProvider):
        def generate(self, request):
            self.requests.append(request)
            raise OwnerChatProviderUnavailable(usage_uncertain=False)

    provider = UnavailableProvider([])
    app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    for index in range(3):
        assert (
            submit(
                api_client, user, business["id"], "kifak", f"failed-{index}"
            ).status_code
            == 503
        )
        assert (
            submit(
                api_client, user, business["id"], "kifak", f"failed-{index}"
            ).status_code
            == 409
        )
    blocked = submit(api_client, user, business["id"], "kifak", "blocked")
    assert blocked.status_code == 429
    assert len(provider.requests) == 3
    with migration_engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT count(*) FROM owner_chat_rate_limit_events"))
            == 3
        )


def test_development_admission_keeps_database_guard_caps_and_function_security(
    migration_engine, database_engine
):
    from sqlalchemy.exc import DBAPIError

    signature = (
        "public.sou2ai_admit_development_owner_chat_generation"
        "(uuid,uuid,integer,integer,integer)"
    )
    with migration_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT prosecdef, proconfig, pg_get_userbyid(proowner) AS owner, "
                "pg_get_functiondef(oid) AS definition FROM pg_proc "
                "WHERE oid=CAST(:signature AS regprocedure)"
            ),
            {"signature": signature},
        ).one()
        assert row.prosecdef and row.proconfig == ["search_path=pg_catalog"]
        assert row.owner == "sou2ai_migrator"
        assert (
            "pg_catalog.current_database() NOT IN ('sou2ai_dev', 'sou2ai_test')"
            in row.definition
        )
        for role in ("public", "sou2ai_lifecycle_operator"):
            assert not connection.scalar(
                text("SELECT has_function_privilege(:role,:signature,'EXECUTE')"),
                {"role": role, "signature": signature},
            )
    for minute, hour in [(121, 30), (3, 2001), (2, 20), (3, 19)]:
        with database_engine.connect() as connection, pytest.raises(DBAPIError):
            connection.execute(
                text(
                    "SELECT * FROM public."
                    "sou2ai_admit_development_owner_chat_generation("
                    "gen_random_uuid(),gen_random_uuid(),1,:minute,:hour)"
                ),
                {"minute": minute, "hour": hour},
            )


@pytest.mark.parametrize("message", ["kifak", "how are you?"])
def test_first_request_on_fresh_app_provider_and_connection_pool(
    api_client, db_session, migration_engine, message
):
    user, business = active_business(api_client, db_session)
    # Discard all request/session state and provider state, as on backend restart.
    db_session.commit()
    get_session_factory.cache_clear()
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "reply": "Hello!",
                                            "requires_business_knowledge": False,
                                        }
                                    )
                                }
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 2,
                    "totalTokenCount": 12,
                },
            },
        )

    fresh = create_app(get_settings())
    fresh.dependency_overrides[get_owner_chat_provider] = lambda: (
        GeminiOwnerChatProvider(
            api_key="test-only",
            model="gemini-3-flash-preview",
            timeout_seconds=120,
            transport=httpx.MockTransport(respond),
        )
    )
    with TestClient(fresh) as client:
        response = submit(client, user, business["id"], message, "first-fresh")
    assert response.status_code == 200, response.text
    assert response.json()["owner_message"]["generation_state"] == "completed"
    assert response.json()["assistant_message"]["content"] == "Hello!"
    assert len(requests) == 1
    with migration_engine.connect() as connection:
        usage = connection.execute(
            text(
                "SELECT status, total_tokens FROM ai_usage_reservations "
                "WHERE owner_message_id = :message_id"
            ),
            {"message_id": response.json()["owner_message"]["id"]},
        ).one()
    assert usage.status == "completed" and usage.total_tokens == 12


@pytest.mark.parametrize(
    "failure", ["forbidden_tool", "forbidden_arguments", "removed", "unavailable"]
)
def test_pending_selection_revalidates_source_and_preserves_provider_usage(
    api_client, db_session, migration_engine, failure
):
    from app.integrations.operational import OperationalSourceUnavailable

    user, business = active_business(api_client, db_session)
    source = StubSource()
    source.resolution = ProductResolution(
        status="ambiguous",
        matched_by="partial_name",
        candidates=(
            ProductResolutionCandidate(
                external_product_id="a", name="Item A", sku="SKU-A"
            ),
            ProductResolutionCandidate(
                external_product_id="b", name="Item B", sku="SKU-B"
            ),
        ),
        metadata=metadata(rows=2),
    )
    plan = usage_result(
        decision="tool",
        semantic_operation="inventory_product",
        entity_kind="product",
        entity_query="Item",
    )
    next_plan = replace(plan, entity_query="Item A")
    if failure == "forbidden_tool":
        next_plan = replace(next_plan, tool_name="run_sql")
    elif failure == "forbidden_arguments":
        next_plan = replace(next_plan, tool_arguments={"url": "https://example.com"})
    provider = SequenceProvider([plan, next_plan])
    configure_operational_chat(db_session, business["id"], source, provider)
    first = submit(api_client, user, business["id"], "show me Item", "ambiguous")
    assert first.status_code == 200, first.text
    if failure == "removed":
        source.resolution = ProductResolution(
            status="not_found", metadata=metadata(rows=0)
        )
    elif failure == "unavailable":

        def unavailable(_query):
            raise OperationalSourceUnavailable("Source unavailable.")

        source.resolve_product = unavailable
    response = submit(api_client, user, business["id"], "SKU-A", "selection")
    assert response.status_code == (503 if failure.startswith("forbidden") else 200), (
        response.text
    )
    assert source.calls == []
    with migration_engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT sum(total_tokens) FROM ai_usage_reservations")
            )
            == 24
        )
        assert connection.scalar(text("SELECT count(*) FROM tool_call_logs")) == 1


@pytest.mark.parametrize(
    "reference",
    [
        "Pepsi Bottle 1.5 L",
        "PEPSI-1500",
        "show me Pepsi Bottle 1.5 L in Achrafieh",
        "the pepsi bottle 1.5 L in achrafieh",
        "Pepsi Bottle 1.5L",
        "the pepsi 1.5L in achrafieh",
    ],
)
def test_specific_product_reference_excludes_weaker_token_matches(
    operational_adapter,  # noqa: F811 - imported pytest fixture
    reference,
):
    result = operational_adapter.resolve_product(
        ProductResolutionQuery(reference=reference)
    )
    assert result.status == "resolved"
    assert result.product.sku == "PEPSI-1500"


def test_first_operational_request_with_cold_source_and_real_gemini_adapter(
    api_client, db_session, migration_engine
):
    user, business = active_business(api_client, db_session)
    configure_operational_chat(
        db_session, business["id"], StubSource(), SequenceProvider([])
    )
    settings = audit_settings()
    # Only the external HTTP response is simulated. Payload building, validation,
    # source initialization, admission, reservation, execution and auditing are real.
    registry = EnvironmentConnectionProfileRegistry(settings)
    calls = []

    def respond(request):
        calls.append(request)
        structured = (
            {
                "decision": "tool",
                "semantic_operation": "inventory_product",
                "entity_kind": "product",
                "entity_query": "Pepsi Bottle 1.5 L",
                "tool_name": "current_inventory",
                "arguments": {"location_reference": "Achrafieh"},
            }
            if len(calls) == 1
            else {
                "reply": "Validated inventory.",
                "validated_result_status": "data",
            }
        )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"parts": [{"text": json.dumps(structured)}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 2,
                    "totalTokenCount": 12,
                },
            },
        )

    fresh = create_app(settings)
    fresh.dependency_overrides[get_settings] = lambda: settings
    fresh.dependency_overrides[get_connection_profile_registry] = lambda: registry
    fresh.dependency_overrides[get_owner_chat_provider] = lambda: (
        GeminiOwnerChatProvider(
            api_key="test-only",
            model="gemini-3-flash-preview",
            timeout_seconds=120,
            transport=httpx.MockTransport(respond),
        )
    )
    get_session_factory.cache_clear()
    get_engine().dispose()
    get_engine.cache_clear()
    try:
        with TestClient(fresh) as client:
            response = submit(
                client,
                user,
                business["id"],
                "show me Pepsi Bottle 1.5 L in Achrafieh",
                "cold-operational",
            )
        assert response.status_code == 200, response.text
        assert response.json()["owner_message"]["generation_state"] == "completed"
        assert len(calls) == 2
        with migration_engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM tool_call_logs")) == 1
            assert (
                connection.scalar(
                    text("SELECT total_tokens FROM ai_usage_reservations")
                )
                == 24
            )
    finally:
        registry.resolve(FAKE_STORE_PROFILE.key).dispose()


@pytest.mark.parametrize(
    "invalidate",
    [
        "expired",
        "source_changed",
        "source_swapped",
        "another_user",
        "another_conversation",
        "another_business",
        "intervening_turn",
        "malformed",
    ],
)
def test_product_clarification_is_bounded_and_scoped(
    api_client, db_session, invalidate
):
    user, business = active_business(api_client, db_session)
    source = StubSource()
    source.resolution = ProductResolution(
        status="ambiguous",
        matched_by="partial_name",
        candidates=(
            ProductResolutionCandidate(
                external_product_id="a", name="Item A", sku="SKU-A"
            ),
            ProductResolutionCandidate(
                external_product_id="b", name="Item B", sku="SKU-B"
            ),
        ),
        metadata=metadata(rows=2),
    )
    configure_operational_chat(
        db_session,
        business["id"],
        source,
        SequenceProvider(
            [
                usage_result(
                    decision="tool",
                    semantic_operation="inventory_product",
                    entity_kind="product",
                    entity_query="Item",
                ),
            ]
        ),
    )
    response = submit(api_client, user, business["id"], "show me Item", "ambiguous")
    assert response.status_code == 200, response.text
    origin = db_session.get(OwnerChatMessage, response.json()["owner_message"]["id"])
    state = dict(origin.operational_clarification)
    if invalidate == "expired":
        state["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif invalidate == "source_changed":
        state["source_updated_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    elif invalidate == "source_swapped":
        state["source_id"] = str(uuid.uuid4())
    elif invalidate == "another_user":
        state["user_id"] = str(uuid.uuid4())
    elif invalidate == "malformed":
        state["arguments"]["arbitrary_sql"] = "select 1"
    origin.operational_clarification = state
    conversation_id = origin.conversation_id
    business_id = uuid.UUID(str(business["id"]))
    if invalidate in {"another_conversation", "another_business"}:
        from app.services.conversations import create_conversation

        conversation_user = user
        if invalidate == "another_business":
            conversation_user, other = active_business(
                api_client,
                db_session,
                email="other-pending@example.com",
                name="Other Market",
            )
            business_id = uuid.UUID(str(other["id"]))
        conversation_id = create_conversation(
            db_session, conversation_user, business_id
        ).id
    followup = OwnerChatMessage(
        conversation_id=conversation_id,
        sequence_number=5 if invalidate == "intervening_turn" else 3,
        role="owner",
        content="SKU-A",
        idempotency_key="selection",
        generation_state="pending",
    )
    db_session.add(followup)
    db_session.flush()
    executor = owner_chat.OperationalToolExecutor(
        db_session, StubRegistry(source), audit_settings()
    )
    assert (
        owner_chat._pending_product_clarification(
            db_session,
            executor,
            user,
            business_id,
            followup.id,
        )
        is None
    )
    db_session.rollback()


@pytest.mark.parametrize(
    "clarify,selection",
    [
        (False, None),
        (True, "PEPSI-1500"),
        (True, "Pepsi Bottle 1.5 L (PEPSI-1500)"),
    ],
)
def test_inventory_override_survives_product_clarification_and_default_stays_saved(
    api_client, db_session, migration_engine, clarify, selection
):
    user, business = active_business(api_client, db_session)
    source = StubSource()
    source.locations = (
        source.locations[0],
        source.locations[1].model_copy(
            update={
                "external_location_id": "BR-ACH",
                "label": "Achrafieh Branch",
            }
        ),
    )
    product = inventory_item().product.model_copy(
        update={
            "external_product_id": "P1008",
            "sku": "PEPSI-1500",
            "name": "Pepsi Bottle 1.5 L",
        }
    )
    candidates = (
        ProductResolutionCandidate(**product.model_dump(exclude={"category"})),
        ProductResolutionCandidate(
            external_product_id="P1007", sku="PEPSI-330", name="Pepsi Can 330 ml"
        ),
    )

    def resolve(query):
        source.resolution_references.append(query.reference)
        # Deliberately exercise genuine ambiguity even when the planner drops location.
        if query.reference == "Pepsi":
            return ProductResolution(
                status="ambiguous",
                matched_by="partial_name",
                candidates=candidates,
                metadata=metadata(rows=2),
            )
        return ProductResolution(
            status="resolved", matched_by="sku", product=product, metadata=metadata()
        )

    source.resolve_product = resolve
    plan = usage_result(
        decision="tool",
        semantic_operation="inventory_product",
        entity_kind="product",
        entity_query="Pepsi",
    )
    results = [
        usage_result(decision="set_preference"),
        plan,
        usage_result(reply="Inventory."),
        plan,
    ]
    if clarify:
        # A provider that loses the follow-up operation must not lose safe context.
        results.append(usage_result(reply="Clarify", semantic_operation="unsupported"))
    results.extend(
        [usage_result(reply="Inventory."), plan, usage_result(reply="Inventory.")]
    )
    provider = SequenceProvider(results)
    configure_operational_chat(db_session, business["id"], source, provider)
    messages = [
        "from now on just answer from Jbeil branch",
        "what i have Pepsi Bottle 1.5 L",
        "show me Pepsi in Achrafieh"
        if clarify
        else "show me Pepsi Bottle 1.5 L in Achrafieh",
        *([selection] if clarify else []),
        "what i have Pepsi Bottle 1.5 L",
    ]
    for index, message in enumerate(messages):
        # Isolated test counters; the production limit remains in place.
        with migration_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM owner_chat_rate_limit_events WHERE business_id=:id"),
                {"id": business["id"]},
            )
        response = submit(
            api_client, user, business["id"], message, f"sequence-{index}"
        )
        assert response.status_code == 200, response.text
        if index == 1 or index == len(messages) - 1:
            assert source.last_inventory_query.branch_external_id == "BR-JBEIL"
        if index == 2 and clarify:
            assert "Which one" in response.json()["assistant_message"]["content"]
            origin = db_session.get(
                OwnerChatMessage, response.json()["owner_message"]["id"]
            )
            assert (
                origin.operational_clarification["arguments"]["branch_external_id"]
                == "BR-ACH"
            )
        if index == (3 if clarify else 2):
            assert source.last_inventory_query.external_product_id == "P1008"
            assert source.last_inventory_query.branch_external_id == "BR-ACH"
        saved = db_session.scalar(select(UserOperationalPreference))
        assert saved.location_external_id == "BR-JBEIL"


@pytest.mark.parametrize("message", ["kifak", "hello", "how are you?"])
def test_connected_source_routes_typed_casual_intent_to_conversation(
    api_client, db_session, message
):
    user, business = active_business(api_client, db_session)
    source = StubSource()
    provider = SequenceProvider(
        [
            usage_result(reply="Hello!", semantic_operation="conversation"),
            OwnerChatResult(reply="I'm here to help. How are you?"),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    response = submit(api_client, user, business["id"], message, "greeting")
    assert response.status_code == 200, response.text
    assert response.json()["assistant_message"]["content"] == provider.results[1].reply
    assert provider.requests[-1].mode == "conversation"
    assert provider.requests[-1].tools == ()
    assert source.calls == []


def test_incomplete_preference_resolves_unique_location_from_current_message(
    api_client, db_session, migration_engine
):
    user, business = active_business(api_client, db_session)
    source = StubSource()
    source.locations = (
        source.locations[0],
        source.locations[1].model_copy(
            update={"external_location_id": "BR-ACH", "label": "Achrafieh Branch"}
        ),
    )
    provider = SequenceProvider(
        [
            usage_result(decision="set_preference"),
            usage_result(
                decision="tool",
                semantic_operation="inventory_product",
                entity_kind="product",
                entity_query="Pepsi Bottle 1.5 L",
            ),
            usage_result(reply="Validated inventory."),
            usage_result(
                decision="tool",
                semantic_operation="inventory_product",
                entity_kind="product",
                entity_query="Pepsi",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={"location_reference": "Achrafieh"},
            ),
            usage_result(reply="Validated inventory."),
            usage_result(decision="clear_preference"),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    response = submit(
        api_client,
        user,
        business["id"],
        "from now on just answer from Jbeil branch",
        "set",
    )
    assert response.status_code == 200, response.text
    saved = db_session.scalar(select(UserOperationalPreference))
    assert saved is not None and saved.location_external_id == "BR-JBEIL"
    response = submit(
        api_client, user, business["id"], "what i have Pepsi Bottle 1.5 L", "stock"
    )
    assert response.status_code == 200, response.text
    assert source.last_inventory_query.branch_external_id == "BR-JBEIL"
    response = submit(
        api_client, user, business["id"], "show me Pepsi in Achrafieh", "override"
    )
    assert response.status_code == 200, response.text
    assert source.last_inventory_query.branch_external_id == "BR-ACH"
    db_session.refresh(saved)
    assert saved.location_external_id == "BR-JBEIL"
    # Reset only the test admission counters for the fourth turn.
    with migration_engine.begin() as connection:
        connection.execute(
            text("DELETE FROM owner_chat_rate_limit_events WHERE business_id = :id"),
            {"id": business["id"]},
        )
    response = submit(
        api_client, user, business["id"], "remove the location preference", "clear"
    )
    assert response.status_code == 200, response.text
    assert "cleared" in response.json()["assistant_message"]["content"]
    assert db_session.scalar(select(UserOperationalPreference)) is None


def test_preference_no_match_is_not_mislabeled_as_ambiguous(api_client, db_session):
    user, business = active_business(api_client, db_session)
    provider = SequenceProvider(
        [
            usage_result(decision="set_preference", location_reference="Atlantis"),
            usage_result(preference_resolution_status="no_match"),
        ]
    )
    configure_operational_chat(db_session, business["id"], StubSource(), provider)
    response = submit(
        api_client, user, business["id"], "use Atlantis as my default", "no-match"
    )
    assert response.status_code == 200, response.text
    assert "couldn't match" in response.json()["assistant_message"]["content"]
    assert db_session.scalar(select(UserOperationalPreference)) is None
    pending = db_session.scalar(select(PendingOwnerOperationalPreference))
    assert pending is not None and pending.state == "pending"


@pytest.mark.parametrize("pending_followup", [False, True])
def test_literal_location_ambiguity_cannot_be_overruled_by_provider(
    api_client, db_session, pending_followup
):
    user, business = active_business(api_client, db_session)
    source = StubSource()
    source.locations = tuple(
        candidate.model_copy(update={"label": f"Beirut {suffix}"})
        for candidate, suffix in zip(
            source.locations[:2], ("Branch", "Warehouse"), strict=True
        )
    )
    prefix = (
        [
            usage_result(decision="set_preference"),
            usage_result(
                preference_resolution_status="ambiguous",
                preference_location_candidate_references=("location_1", "location_2"),
            ),
        ]
        if pending_followup
        else []
    )
    provider = SequenceProvider(
        prefix
        + [
            (
                usage_result(reply="Select", semantic_operation="unsupported")
                if pending_followup
                else usage_result(
                    decision="set_preference", location_reference="Beirut"
                )
            ),
            usage_result(
                preference_resolution_status="matched",
                preference_resolution_key="default_inventory_location",
                preference_location_candidate_references=("location_1",),
            ),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    if pending_followup:
        first = submit(
            api_client, user, business["id"], "use a location by default", "pending"
        )
        assert first.status_code == 200, first.text
    response = submit(
        api_client,
        user,
        business["id"],
        "Beirut" if pending_followup else "use Beirut by default",
        "ambiguous",
    )
    assert response.status_code == 200, response.text
    assert db_session.scalar(select(UserOperationalPreference)) is None
    content = response.json()["assistant_message"]["content"]
    assert "Beirut Branch" in content and "Beirut Warehouse" in content
    assert source.calls == []


def test_full_source_location_label_disambiguates_same_city_candidates():
    source = StubSource()
    candidates = tuple(
        candidate.model_copy(update={"label": f"Beirut {suffix}"})
        for candidate, suffix in zip(
            source.locations[:2], ("Branch", "Warehouse"), strict=True
        )
    )
    assert owner_chat._preference_locations_in_message(
        "from now on just answer from Beirut branch", candidates
    ) == (candidates[0],)


@pytest.mark.parametrize(
    "phrase", ["last month", "previous month", "latest completed month"]
)
@pytest.mark.parametrize(
    "requested_at, start, end",
    [
        (datetime(2026, 9, 30, 12, tzinfo=UTC), date(2026, 8, 1), date(2026, 9, 1)),
        (
            datetime(2026, 12, 31, 22, 30, tzinfo=UTC),
            date(2026, 12, 1),
            date(2027, 1, 1),
        ),
        (datetime(2028, 3, 10, 12, tzinfo=UTC), date(2028, 2, 1), date(2028, 3, 1)),
    ],
)
def test_revenue_month_is_normalized_by_backend(
    api_client, db_session, monkeypatch, phrase, requested_at, start, end
):
    user, business = active_business(api_client, db_session)
    source = StubSource()
    provider = SequenceProvider(
        [
            usage_result(reply="Clarify dates", semantic_operation="sales_summary"),
            usage_result(reply="Report complete."),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    monkeypatch.setattr(owner_chat, "utc_now", lambda: requested_at)
    response = submit(
        api_client,
        user,
        business["id"],
        f"give me the revenue in the {phrase}",
        "month",
    )
    assert response.status_code == 200, response.text
    assert source.calls == [SALES_SUMMARY_TOOL]
    result = provider.requests[-1].tool_results[0].output
    assert result["period"]["start_date"] == start.isoformat()
    assert result["period"]["end_date"] == end.isoformat()
    assert result["metric"] == "revenue"


@pytest.mark.parametrize("failure", ["invalid", "timeout", "unavailable", "malformed"])
def test_sales_synthesis_failure_still_returns_validated_revenue(
    api_client, db_session, failure
):
    user, business = active_business(api_client, db_session)
    source = StubSource()
    plan = usage_result(
        decision="tool",
        semantic_operation="sales_summary",
        tool_name=SALES_SUMMARY_TOOL,
        tool_arguments={
            "metric": "revenue",
            "start_date": "2026-08-01",
            "end_date": "2026-09-01",
        },
    )
    provider = (
        FailingSecondProvider([plan])
        if failure == "timeout"
        else SequenceProvider([plan, usage_result(reply="")])
    )
    if failure in {"unavailable", "malformed"}:

        class FailedSynthesisProvider(SequenceProvider):
            def generate(self, request):
                if self.requests:
                    self.requests.append(request)
                    if failure == "unavailable":
                        raise OwnerChatProviderUnavailable(usage_uncertain=False)
                    raise OwnerChatProviderInvalidResponse(
                        reason="schema_validation_failed"
                    )
                return super().generate(request)

        provider = FailedSynthesisProvider([plan])
    configure_operational_chat(db_session, business["id"], source, provider)
    response = submit(
        api_client, user, business["id"], "revenue for August 2026", "revenue"
    )
    assert response.status_code == 200, response.text
    content = response.json()["assistant_message"]["content"]
    assert "2000000" in content and "LBP" in content
    assert "2026-08-01" in content and "2026-09-01" in content
    assert "net revenue" in content.casefold()
    assert source.calls == [SALES_SUMMARY_TOOL]


def test_current_product_price_is_truthfully_unsupported(api_client, db_session):
    user, business = active_business(api_client, db_session)
    source = StubSource()
    provider = SequenceProvider(
        [usage_result(reply="Pepsi costs 99", semantic_operation="product_price")]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    response = submit(
        api_client,
        user,
        business["id"],
        "what is the price of Pepsi Bottle 1.5 L?",
        "price",
    )
    assert response.status_code == 200, response.text
    content = response.json()["assistant_message"]["content"]
    assert "current product prices" in content.casefold()
    assert "99" not in content and "safe lookup" not in content
    assert source.calls == []


def test_typed_sales_result_has_deterministic_formatter_without_provider():
    result = StubSource().get_sales_summary(
        SalesQuery(start_date=date(2026, 8, 1), end_date=date(2026, 9, 1))
    )
    formatted = owner_chat._operational_synthesis_fallback(
        result, usage_result().usage, "offline-test", "sequence"
    )
    assert "net revenue" in formatted.reply.casefold()
    assert "2000000" in formatted.reply
    assert "2026-08-01" in formatted.reply and "2026-09-01" in formatted.reply


def test_sales_clarification_followup_is_bounded_to_one_recent_turn(
    api_client, db_session
):
    user, business = active_business(api_client, db_session)
    source = StubSource()
    provider = SequenceProvider(
        [
            usage_result(reply="Clarify", semantic_operation="sales_summary"),
            usage_result(
                decision="tool",
                semantic_operation="sales_summary",
                tool_arguments={
                    "metric": "revenue",
                    "date_range": "previous_completed_month",
                    "use_pending_clarification": True,
                },
            ),
            usage_result(reply="Validated revenue."),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    first = submit(api_client, user, business["id"], "give me the sales", "sales")
    assert first.status_code == 200, first.text
    assert "revenue, sales count" in first.json()["assistant_message"]["content"]
    assert source.calls == []
    followup = submit(
        api_client, user, business["id"], "revenue for last month", "followup"
    )
    assert followup.status_code == 200, followup.text
    assert provider.requests[1].pending_sales_clarification is True
    assert len(provider.requests[1].messages) == 1
    assert provider.requests[1].rolling_summary is None
    assert source.calls == [SALES_SUMMARY_TOOL]


def test_vague_sales_followup_clarifies_instead_of_failing(api_client, db_session):
    user, business = active_business(api_client, db_session)
    source = StubSource()
    provider = SequenceProvider(
        [
            usage_result(reply="Clarify", semantic_operation="sales_summary"),
            usage_result(reply="Clarify", semantic_operation="sales_summary"),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    assert (
        submit(
            api_client, user, business["id"], "give me the sales", "sales"
        ).status_code
        == 200
    )
    response = submit(
        api_client,
        user,
        business["id"],
        "the available sales metric and latest date range",
        "followup",
    )
    assert response.status_code == 200, response.text
    assert "revenue, sales count" in response.json()["assistant_message"]["content"]
    assert source.calls == []


@pytest.mark.parametrize("invalidate", ["expired", "source_changed", "unrelated"])
def test_sales_pending_state_expires_and_cannot_leak_constraints(
    api_client, db_session, invalidate
):
    user, business = active_business(api_client, db_session)
    provider = SequenceProvider(
        [usage_result(reply="Clarify", semantic_operation="sales_summary")]
    )
    source = StubSource()
    configure_operational_chat(db_session, business["id"], source, provider)
    first = submit(api_client, user, business["id"], "give me the sales", "sales")
    assert first.status_code == 200, first.text
    assistant = db_session.get(
        OwnerChatMessage, first.json()["assistant_message"]["id"]
    )
    # Use the actual persisted timestamp only to construct controlled request times.
    request_time = assistant.created_at + timedelta(seconds=1)
    source_updated_at = assistant.created_at - timedelta(seconds=1)
    owner = OwnerChatMessage(
        conversation_id=assistant.conversation_id,
        sequence_number=assistant.sequence_number
        + (3 if invalidate == "unrelated" else 1),
        role="owner",
        content="revenue last month",
        idempotency_key="next",
        generation_state="pending",
    )
    db_session.add(owner)
    db_session.flush()
    if invalidate == "expired":
        request_time += timedelta(minutes=16)
    if invalidate == "source_changed":
        source_updated_at = assistant.created_at + timedelta(seconds=1)
    assert (
        owner_chat._pending_sales_clarification(
            db_session, owner.id, request_time, source_updated_at
        )
        is False
    )
    db_session.rollback()


@pytest.mark.parametrize("semantic", ["conversation", "knowledge"])
def test_live_request_cannot_be_answered_from_a_nonoperational_plan(
    api_client, db_session, semantic
):
    user, business = active_business(api_client, db_session)
    source = StubSource()
    provider = SequenceProvider(
        [usage_result(reply="There are 999 units", semantic_operation=semantic)]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    response = submit(
        api_client, user, business["id"], "what is current stock?", "live"
    )
    assert response.status_code == 200, response.text
    content = response.json()["assistant_message"]["content"]
    assert "safe lookup" in content
    assert "999" not in content and source.calls == []


def test_failed_synthesis_validation_keeps_authoritative_usage():
    request = replace(
        operational_request(),
        mode="operational_synthesis",
        validated_result_status="data",
    )
    provider = SequenceProvider([usage_result(reply="")])
    with pytest.raises(OwnerChatProviderInvalidResponse) as error:
        owner_chat._validated_generation(provider, request)
    assert error.value.usage == provider.results[0].usage
    assert (
        owner_chat._provider_failure_usage(provider, request, error.value).total_tokens
        == 12
    )


def test_pre_dispatch_synthesis_unavailability_has_zero_failure_charge():
    request = replace(operational_request(), mode="operational_synthesis")
    assert (
        owner_chat._provider_failure_usage(
            SequenceProvider([]),
            request,
            OwnerChatProviderUnavailable(usage_uncertain=False),
        ).total_tokens
        == 0
    )


@pytest.mark.parametrize(
    "phrase", ["last month", "previous month", "latest completed month"]
)
def test_month_boundaries_override_rolling_or_incorrect_provider_dates(phrase):
    request = replace(
        operational_request(),
        requested_at=datetime(2028, 3, 1, tzinfo=UTC),
        reporting_timezone="Asia/Beirut",
    )
    assert owner_chat._completed_month_period(phrase, request) == (
        "2028-02-01",
        "2028-03-01",
    )


def test_empty_and_zero_inventory_are_not_conflated():
    item = inventory_item().model_copy(
        update={"on_hand_quantity": 0, "reserved_quantity": 0, "available_quantity": 0}
    )
    zero = InventoryResult(items=(item,), metadata=metadata())
    empty = InventoryResult(items=(), metadata=metadata(rows=0))
    assert (
        "0 available"
        in owner_chat._operational_synthesis_fallback(
            zero, usage_result().usage, None, None
        ).reply
    )
    assert (
        "No matching inventory rows"
        in owner_chat._operational_synthesis_fallback(
            empty, usage_result().usage, None, None
        ).reply
    )


def test_no_match_location_can_be_clarified_and_changed_from_bounded_candidates(
    api_client, db_session
):
    user, business = active_business(api_client, db_session)
    source = StubSource()
    source.locations = (
        source.locations[0],
        source.locations[1].model_copy(
            update={"external_location_id": "BR-ACH", "label": "Achrafieh Branch"}
        ),
    )
    provider = SequenceProvider(
        [
            usage_result(decision="set_preference", location_reference="Atlantis"),
            usage_result(preference_resolution_status="no_match"),
            usage_result(reply="Clarify"),
            usage_result(
                preference_resolution_status="matched",
                preference_resolution_key="default_inventory_location",
                preference_location_candidate_references=("location_2",),
            ),
            usage_result(decision="set_preference", location_reference="Jbeil branch"),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    first = submit(
        api_client, user, business["id"], "answer from Atlantis by default", "unknown"
    )
    assert (
        first.status_code == 200
        and "couldn't match" in first.json()["assistant_message"]["content"]
    )
    selected = submit(api_client, user, business["id"], "achrafieh", "selected")
    assert selected.status_code == 200, selected.text
    saved = db_session.scalar(select(UserOperationalPreference))
    assert saved is not None and saved.location_external_id == "BR-ACH"
    changed = submit(
        api_client, user, business["id"], "use Jbeil branch from now on", "changed"
    )
    assert changed.status_code == 200, changed.text
    db_session.refresh(saved)
    assert saved.location_external_id == "BR-JBEIL"
    assert source.calls == []


def test_casual_conversation_survives_unhealthy_connected_source(
    api_client, db_session
):
    user, business = active_business(api_client, db_session)
    source = StubSource()
    source.health_error = RuntimeError("unavailable")
    provider = SequenceProvider(
        [
            usage_result(reply="Hello", semantic_operation="conversation"),
            OwnerChatResult(reply="Hello, how are you?"),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    response = submit(api_client, user, business["id"], "kifak", "casual")
    assert response.status_code == 200, response.text
    assert response.json()["assistant_message"]["content"] == "Hello, how are you?"
    assert source.calls == []


def test_new_preference_command_supersedes_pending_and_can_be_cleared(
    api_client, db_session
):
    user, business = active_business(api_client, db_session)
    source = StubSource()
    provider = SequenceProvider(
        [
            usage_result(decision="set_preference"),
            usage_result(
                preference_resolution_status="ambiguous",
                preference_location_candidate_references=("location_1", "location_2"),
            ),
            usage_result(decision="set_preference", location_reference="Jbeil"),
            usage_result(decision="clear_preference"),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    first = submit(
        api_client, user, business["id"], "use a branch for future inventory", "pending"
    )
    assert first.status_code == 200, first.text
    pending = db_session.scalar(select(PendingOwnerOperationalPreference))
    assert pending is not None and pending.state == "pending"
    changed = submit(
        api_client,
        user,
        business["id"],
        "instead use Jbeil branch by default",
        "changed",
    )
    assert changed.status_code == 200, changed.text
    db_session.refresh(pending)
    assert pending.state == "superseded"
    saved = db_session.scalar(select(UserOperationalPreference))
    assert saved is not None and saved.location_external_id == "BR-JBEIL"
    cleared = submit(
        api_client, user, business["id"], "remove the location preference", "clear"
    )
    assert cleared.status_code == 200, cleared.text
    assert db_session.scalar(select(UserOperationalPreference)) is None
    assert source.calls == []


def test_connected_source_knowledge_route_preserves_grounding_and_citations(
    api_client, db_session, monkeypatch
):
    import uuid

    from tests.test_rag_lifecycle import Provider, ready_document, vector

    user, business = active_business(api_client, db_session)
    ready_document(
        db_session,
        uuid.UUID(business["id"]),
        digest="4" * 64,
        chunks=[("Returns are accepted within 14 days.", vector(1), "bge-m3")],
    )
    source = StubSource()
    provider = SequenceProvider(
        [
            usage_result(reply="Use stable knowledge", semantic_operation="knowledge"),
            OwnerChatResult(
                reply="Returns are accepted within 14 days.", cited_source_ids=("S1",)
            ),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    monkeypatch.setattr(owner_chat, "create_embedding_provider", lambda _: Provider())
    response = submit(
        api_client, user, business["id"], "what is our return policy?", "policy"
    )
    assert response.status_code == 200, response.text
    assert provider.requests[-1].mode == "grounded"
    assert response.json()["assistant_message"]["sources"][0]["label"] == "S1"
    assert source.calls == []


def test_original_scenarios_against_live_fake_store(
    api_client,
    db_session,
    migration_engine,
    operational_adapter,  # noqa: F811 - imported pytest fixture
    monkeypatch,
):
    """Exercise the original ten-scenario sequence with real controlled reads."""
    user, business = active_business(api_client, db_session)
    product = "Pepsi Bottle 1.5 L"
    inventory_plan = usage_result(
        decision="tool",
        semantic_operation="inventory_product",
        entity_kind="product",
        entity_query=product,
    )
    sales_plan = usage_result(
        decision="tool",
        semantic_operation="sales_summary",
        tool_arguments={"metric": "revenue", "date_range": "previous_completed_month"},
    )
    provider = SequenceProvider(
        [
            usage_result(reply="Greet", semantic_operation="conversation"),
            OwnerChatResult(reply="I'm here to help. Kifak enta?"),
            usage_result(reply="Unsupported price", semantic_operation="product_price"),
            usage_result(decision="set_preference", location_reference="Jbeil branch"),
            inventory_plan,
            usage_result(reply=""),
            replace(
                inventory_plan,
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={"location_reference": "Achrafieh"},
            ),
            usage_result(reply=""),
            usage_result(decision="clear_preference"),
            sales_plan,
            usage_result(reply=""),
            usage_result(reply="Clarify", semantic_operation="sales_summary"),
            replace(
                sales_plan,
                tool_arguments={
                    "metric": "revenue",
                    "date_range": "previous_completed_month",
                    "use_pending_clarification": True,
                },
            ),
            usage_result(reply=""),
            inventory_plan,
            usage_result(reply=""),
        ]
    )
    configure_operational_chat(
        db_session, business["id"], operational_adapter, provider
    )
    active = db_session.scalar(select(OperationalDataSourceConfig))
    assert active is not None
    requested_at = active.updated_at + timedelta(minutes=1)
    monkeypatch.setattr(owner_chat, "utc_now", lambda: requested_at)
    expected_end = (
        requested_at.astimezone(ZoneInfo("Asia/Beirut")).date().replace(day=1)
    )
    expected_start = (expected_end - timedelta(days=1)).replace(day=1)
    messages = [
        "kifak",
        f"what is the price of {product}?",
        "from now on just answer from Jbeil branch",
        f"what i have {product}",
        f"show me {product} in Achrafieh",
        "remove the location preference",
        "give me the revenue in the last month",
        "give me the sales",
        "revenue for last month",
        f"show me {product}",
    ]
    answers = []
    for index, message in enumerate(messages):
        # Test-only admission reset, scoped to this isolated fixture business.
        with migration_engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM owner_chat_rate_limit_events WHERE business_id = :id"
                ),
                {"id": business["id"]},
            )
        response = submit(
            api_client, user, business["id"], message, f"scenario-{index}"
        )
        assert response.status_code == 200, f"Scenario {index + 1}: {response.text}"
        answers.append(response.json()["assistant_message"]["content"])
        with migration_engine.begin() as connection:
            connection.execute(
                text("UPDATE owner_chat_messages SET created_at = :at WHERE id = :id"),
                {"at": requested_at, "id": response.json()["assistant_message"]["id"]},
            )
        db_session.expire_all()
        saved = db_session.scalar(select(UserOperationalPreference))
        if index in {2, 3, 4}:
            assert saved is not None and saved.location_external_id == "BR-JBEIL"
        if index >= 5:
            assert saved is None
        if index == 8:
            assert "net revenue" in answers[index], (
                answers[index],
                provider.requests[-1].pending_sales_clarification,
            )
    assert "safe lookup" not in answers[0]
    assert "current product prices" in answers[1]
    assert "saved" in answers[2]
    assert "Jbeil Branch" in answers[3] and "Achrafieh" not in answers[3]
    assert "Achrafieh Branch" in answers[4] and "Jbeil" not in answers[4]
    assert "cleared" in answers[5]
    assert "net revenue" in answers[6] and expected_start.isoformat() in answers[6]
    assert expected_end.isoformat() in answers[6]
    assert "revenue, sales count" in answers[7]
    assert "net revenue" in answers[8]
    assert "Current inventory" in answers[9]
    assert all("could not safely format" not in answer for answer in answers)
    operational_plans = [
        request for request in provider.requests if request.mode == "operational"
    ]
    assert operational_plans[8].pending_sales_clarification is True
    assert operational_plans[9].pending_sales_clarification is False
    assert all(
        len(request.messages) == 1 and request.rolling_summary is None
        for request in operational_plans
    )
