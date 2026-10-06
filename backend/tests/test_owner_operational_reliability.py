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
    TokenUsage,
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
    CategoryCandidate,
    CategoryResolution,
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


def test_connected_source_delegates_a_replyless_conversation_plan(
    api_client, db_session
):
    user, business = active_business(api_client, db_session)
    source = StubSource()
    provider = SequenceProvider(
        [
            usage_result(reply="", semantic_operation="conversation"),
            OwnerChatResult(reply="I'm here to help. How are you?"),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    response = submit(api_client, user, business["id"], "kifak", "replyless-greeting")
    assert response.status_code == 200
    assert response.json()["assistant_message"]["content"] == provider.results[1].reply
    assert [request.mode for request in provider.requests] == [
        "operational",
        "conversation",
    ]
    assert source.calls == []


def test_best_sellers_semantic_derives_registered_bounded_command():
    from types import SimpleNamespace

    from app.tools.operational import build_operational_tool_registry

    executor = SimpleNamespace(
        registry=build_operational_tool_registry(timeout_seconds=2)
    )
    plan = usage_result(
        decision="tool",
        semantic_operation="best_selling_products",
        tool_name="best_selling_products",
        tool_arguments={
            "start_date": "2026-08-01",
            "end_date": "2026-09-01",
            "branch_external_id": "BR-JBEIL",
            "limit": 3,
        },
    )
    normalized, command = owner_chat._consistent_operational_plan(plan, executor, ())
    assert command.tool_name == "best_selling_products"
    assert normalized.tool_arguments == {
        "start_date": "2026-08-01",
        "end_date": "2026-09-01",
        "metric": "revenue",
        "branch_external_id": "BR-JBEIL",
        "limit": 3,
    }
    _, missing = owner_chat._consistent_operational_plan(
        usage_result(decision="tool", semantic_operation="best_selling_products"),
        executor,
        (),
    )
    assert missing is None


def test_quantity_free_inventory_synthesis_uses_validated_fallback(
    api_client, db_session
):
    user, business = active_business(api_client, db_session)
    source = StubSource()
    provider = SequenceProvider(
        [
            usage_result(decision="tool", semantic_operation="inventory_list"),
            usage_result(
                reply="I have retrieved your inventory. "
                "You can review the stock levels."
            ),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    response = submit(
        api_client, user, business["id"], "Show current inventory", "quantity-free"
    )
    assert response.status_code == 200
    answer = response.json()["assistant_message"]["content"]
    assert str(inventory_item().available_quantity) in answer
    assert inventory_item().branch_name in answer
    assert len(provider.requests) == 2


def test_inventory_presentation_uses_source_quantities_and_discloses_partial_rows(
    api_client, db_session
):
    user, business = active_business(api_client, db_session)
    source = StubSource()
    items = tuple(
        inventory_item().model_copy(
            update={
                "product": inventory_item().product.model_copy(
                    update={
                        "external_product_id": f"item-{index}",
                        "name": f"Source item {index} 1.5 L",
                    }
                )
            }
        )
        for index in range(12)
    )
    source.get_current_inventory = lambda query: InventoryResult(
        items=items,
        metadata=metadata(rows=12).model_copy(update={"is_truncated": True}),
    )
    provider = SequenceProvider(
        [
            usage_result(decision="tool", semantic_operation="inventory_list"),
            usage_result(reply="Here is every item: 999 cases of Source item 0 1.5 L."),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    response = submit(
        api_client,
        user,
        business["id"],
        "Show stock quantities and units",
        "complete-rows",
    )
    assert response.status_code == 200, response.text
    reply = response.json()["assistant_message"]["content"]
    for item in items[:10]:
        assert item.product.name in reply
    for field, label in (
        ("available_quantity", "available"),
        ("on_hand_quantity", "on hand"),
        ("reserved_quantity", "reserved"),
    ):
        assert f"{getattr(items[0], field)} {label}" in reply
    assert "Showing 10 of 12 returned rows" in reply
    assert "source result is limited" in reply
    assert "pack/case units were not supplied" in reply
    assert "999" not in reply and "every item" not in reply


@pytest.mark.parametrize("kind", ["product", "category", "location"])
def test_unresolved_yes_retains_inventory_choices_and_clear_selection_resumes(
    api_client, db_session, kind
):
    user, business = active_business(api_client, db_session)
    source = StubSource()
    product = inventory_item().product
    product_candidates = (
        ProductResolutionCandidate(**product.model_dump(exclude={"category"})),
        ProductResolutionCandidate(
            external_product_id="other-item", name="Other product", sku="OTHER-SKU"
        ),
    )
    if kind == "product":
        source.resolve_product = lambda query: (
            ProductResolution(
                status="ambiguous",
                matched_by="partial_name",
                candidates=product_candidates,
                metadata=metadata(rows=2),
            )
            if query.reference == "product"
            else ProductResolution(
                status="resolved",
                matched_by="external_id",
                product=product,
                metadata=metadata(),
            )
        )
        semantic, phrase, selection = "inventory_product", "product", product.name
        first = usage_result(
            decision="tool",
            semantic_operation=semantic,
            entity_kind="product",
            entity_query=phrase,
            tool_name=CURRENT_INVENTORY_TOOL,
            tool_arguments={"product_filter": phrase, "limit": 7},
        )
        prefix = [first]
        guessed = replace(first, entity_query=selection, pending_reply="confirmation")
    elif kind == "category":
        source.categories = (
            CategoryCandidate(external_category_id="food", label="Seasonal Food"),
            CategoryCandidate(external_category_id="drinks", label="Seasonal Drinks"),
        )
        source.category_resolution = CategoryResolution(
            status="resolved", category=source.categories[0], metadata=metadata()
        )
        semantic, phrase, selection = "inventory_category", "Seasonal", "Seasonal Food"
        first = usage_result(
            decision="tool",
            semantic_operation=semantic,
            entity_kind="category",
            entity_query=phrase,
            tool_name=CURRENT_INVENTORY_TOOL,
            tool_arguments={"category_filter": phrase, "limit": 7},
        )
        prefix = [
            first,
            usage_result(
                category_resolution_status="ambiguous",
                category_candidate_references=("food", "drinks"),
            ),
        ]
        guessed = replace(first, entity_query=selection, pending_reply="confirmation")
    else:
        source.locations = tuple(
            candidate.model_copy(update={"label": f"Test North {suffix}"})
            for candidate, suffix in zip(
                source.locations[:2], ("Branch", "Warehouse"), strict=True
            )
        )
        semantic, phrase, selection = (
            "inventory_list",
            "Test North",
            "Test North Branch",
        )
        first = usage_result(
            decision="tool",
            semantic_operation=semantic,
            tool_name=CURRENT_INVENTORY_TOOL,
            tool_arguments={"location_reference": selection, "limit": 7},
        )
        prefix = [first, usage_result(reply="Choose a location.")]
        guessed = replace(first, pending_reply="confirmation")
    provider = SequenceProvider(
        prefix
        + [
            guessed,
            usage_result(
                reply="", semantic_operation="conversation", pending_reply="selection"
            ),
            usage_result(reply="Stock is 12 units."),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    first_response = submit(
        api_client, user, business["id"], f"Show inventory for {phrase}", "choose"
    )
    assert first_response.status_code == 200, first_response.text
    original = db_session.get(
        OwnerChatMessage, first_response.json()["owner_message"]["id"]
    )
    expires_at = original.operational_clarification["expires_at"]
    reads_before = len(source.calls)
    yes = submit(api_client, user, business["id"], "yes", "unresolved")
    assert yes.status_code == 200, yes.text
    assert len(source.calls) == reads_before
    owner = db_session.get(OwnerChatMessage, yes.json()["owner_message"]["id"])
    assert owner.operational_clarification["expires_at"] == expires_at
    assert selection in yes.json()["assistant_message"]["content"]
    context = provider.requests[-1].pending_clarification
    assert context["expected_reply"] == "selection" and context["operation"] == semantic
    assert context["owner_request"] == f"Show inventory for {phrase}"
    chosen = submit(api_client, user, business["id"], selection, "selected")
    assert chosen.status_code == 200, chosen.text
    assert source.last_inventory_query.limit == 7
    assert len(source.calls) == reads_before + 1


@pytest.mark.parametrize("reason", ["schema_validation_failed", "output_truncated"])
def test_known_rejected_pending_plan_retains_source_choices_and_actual_usage(
    api_client, db_session, migration_engine, reason
):
    user, business = active_business(api_client, db_session)
    source = StubSource()
    source.locations = tuple(
        candidate.model_copy(update={"label": f"Test North {suffix}"})
        for candidate, suffix in zip(
            source.locations[:2], ("Branch", "Warehouse"), strict=True
        )
    )

    class RejectedYesProvider(SequenceProvider):
        def generate(self, request):
            if len(self.requests) == 2:
                self.requests.append(request)
                raise OwnerChatProviderInvalidResponse(
                    reason=reason, usage=usage_result().usage
                )
            return super().generate(request)

    provider = RejectedYesProvider(
        [
            usage_result(
                decision="tool",
                semantic_operation="inventory_list",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={"location_reference": "Test North Branch"},
            ),
            usage_result(reply="Which location?"),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    first = submit(
        api_client,
        user,
        business["id"],
        "Show inventory at Test North",
        "rejected-choice",
    )
    assert first.status_code == 200, first.text
    yes = submit(api_client, user, business["id"], "yes", "rejected-yes")
    assert yes.status_code == 200, yes.text
    assert source.calls == [] and len(provider.requests) == 3
    owner = db_session.get(OwnerChatMessage, yes.json()["owner_message"]["id"])
    assert len(owner.operational_clarification["location_candidates"]) == 2
    assert "Test North Branch" in yes.json()["assistant_message"]["content"]
    with migration_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT total_tokens,counts_authoritative FROM ai_usage_reservations "
                "ORDER BY created_at"
            )
        ).all()
    assert rows == [(24, True), (12, True)]
    replay = submit(api_client, user, business["id"], "yes", "rejected-yes")
    assert replay.status_code == 200 and len(provider.requests) == 3


@pytest.mark.parametrize(
    "reported_usage,unrelated", [(True, False), (False, False), (True, True)]
)
def test_saved_rejected_location_yes_keeps_validated_state_offline(
    monkeypatch, reported_usage, unrelated
):
    from types import SimpleNamespace

    from app.schemas.operational import (
        InventoryQuery,
        LocationCandidate,
        PendingInventoryClarification,
    )

    # Transcribed from retained live call 5; no live artifact or database required.
    call = {
        "raw_text": json.dumps(
            {
                "semantic_operation": "inventory_list",
                "decision": "tool",
                "tool_name": "current_inventory",
                "arguments": {"location_reference": "Evaluation North Branch"},
                "pending_reply": "selection",
                "entity_kind": "product",
                "entity_query": "Evaluation North Branch",
            }
        ),
        "usage": {
            "promptTokenCount": 1482,
            "candidatesTokenCount": 88,
            "totalTokenCount": 1570,
        },
    }
    now = datetime.now(UTC)
    pending = PendingInventoryClarification(
        user_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        source_updated_at=now,
        expires_at=now + timedelta(minutes=15),
        arguments=InventoryQuery(),
        owner_request="Show inventory at Evaluation North",
        location_candidates=(
            LocationCandidate(
                external_location_id="EVAL-BR",
                label="Evaluation North Branch",
                location_type="branch",
            ),
            LocationCandidate(
                external_location_id="EVAL-WH",
                label="Evaluation North Warehouse",
                location_type="warehouse",
            ),
        ),
    )
    context = {
        "operation": "inventory_list",
        "expected_reply": "selection",
        "owner_request": pending.owner_request,
        "question": "Which source location?",
        "candidates": [{"label": item.label} for item in pending.location_candidates],
    }
    if unrelated:
        call["raw_text"] = json.dumps(
            {
                "decision": "final",
                "semantic_operation": "product_price",
                "pending_reply": "unrelated",
            }
        )
    request = replace(
        operational_request(),
        pending_clarification=context,
        messages=(
            owner_chat.ProviderMessage(
                role="owner",
                content=(
                    "What is the current price at Evaluation North Warehouse?"
                    if unrelated
                    else "yes"
                ),
            ),
        ),
    )
    requests = []

    def respond(http_request):
        requests.append(http_request)
        body = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": call["raw_text"]}]},
                }
            ]
        }
        if reported_usage:
            body["usageMetadata"] = call["usage"]
        return httpx.Response(200, json=body)

    provider = GeminiOwnerChatProvider(
        api_key="offline-unused",
        model="gemini-3.1-flash-lite",
        timeout_seconds=120,
        transport=httpx.MockTransport(respond),
    )
    owner = SimpleNamespace(operational_clarification=None)
    session = SimpleNamespace(get=lambda *args: owner, commit=lambda: None)
    executor = SimpleNamespace(
        sales_reporting_context=lambda *args: ("Asia/Beirut", ()),
        _active_source=lambda *args: SimpleNamespace(
            updated_at=pending.source_updated_at
        ),
    )
    monkeypatch.setattr(owner_chat, "_pending_sales_clarification", lambda *args: False)
    monkeypatch.setattr(
        owner_chat, "_pending_product_clarification", lambda *args: pending
    )
    monkeypatch.setattr(
        owner_chat,
        "_build_operational_request",
        lambda *args, **kwargs: SimpleNamespace(
            request=request, business=SimpleNamespace(default_language="en")
        ),
    )
    monkeypatch.setattr(
        owner_chat, "_complete_pending_preference_if_selected", lambda *args: None
    )
    args = (
        session,
        uuid.uuid4(),
        SimpleNamespace(message_id=uuid.uuid4()),
        SimpleNamespace(id=pending.user_id),
        provider,
        get_settings(),
        executor,
        request.tools,
    )
    if unrelated:
        result, usage = owner_chat._run_operational_loop(*args)
        assert "current product prices" in result.reply
        assert owner.operational_clarification is None
    elif reported_usage:
        result, usage = owner_chat._run_operational_loop(*args)
        assert (
            "Evaluation North Branch" in result.reply
            and "Evaluation North Warehouse" in result.reply
        )
        assert (
            owner.operational_clarification["expires_at"]
            == pending.model_dump(mode="json")["expires_at"]
        )
        assert (
            usage.total_tokens == call["usage"]["totalTokenCount"]
            and usage.authoritative
        )
    else:
        with pytest.raises(OwnerChatProviderInvalidResponse):
            owner_chat._run_operational_loop(*args)
        assert owner.operational_clarification is None
    assert len(requests) == 1


@pytest.mark.parametrize("failure", ["reported", "unknown", "pre_use", "rate_limited"])
def test_reservation_sizing_charges_prior_calls_on_delegated_failure(
    api_client, db_session, migration_engine, failure
):
    user, business = active_business(api_client, db_session)

    class FailedDelegation(SequenceProvider):
        def estimate_input_tokens(self, request):
            return 100 if request.mode == "operational" else 900

        def generate(self, request):
            if not self.requests:
                return super().generate(request)
            self.requests.append(request)
            raise OwnerChatProviderUnavailable(
                reason="rate_limited" if failure == "rate_limited" else "test_failure",
                usage=TokenUsage(800, 100, 900, True)
                if failure == "reported"
                else None,
                usage_uncertain=failure != "pre_use",
            )

    provider = FailedDelegation(
        [usage_result(semantic_operation="conversation", reply="")]
    )
    configure_operational_chat(db_session, business["id"], StubSource(), provider)
    response = submit(
        api_client, user, business["id"], "Give me general advice", "delegation-failure"
    )
    assert response.status_code == 503, response.text
    assert len(provider.requests) == 2
    expected = 912 if failure == "reported" else 1424 if failure == "unknown" else 12
    with migration_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT total_tokens,counts_authoritative,status "
                "FROM ai_usage_reservations"
            )
        ).one()
        assert row == (expected, failure == "reported", "charged")
        assert connection.execute(
            text(
                "SELECT total_tokens_used,tokens_reserved FROM business_ai_usage_daily"
            )
        ).one() == (expected, 0)


def test_reservation_sizing_accounts_for_large_serialized_source_results(
    api_client, db_session, migration_engine
):
    user, business = active_business(api_client, db_session)
    estimator = GeminiOwnerChatProvider(
        api_key="offline-unused", model="gemini-3.1-flash-lite", timeout_seconds=120
    )
    estimates = {}

    class LargeSource(StubSource):
        def get_current_inventory(self, query):
            self.calls.append(CURRENT_INVENTORY_TOOL)
            item = inventory_item()
            items = tuple(
                item.model_copy(
                    update={
                        "product": item.product.model_copy(
                            update={
                                "external_product_id": str(index),
                                "name": "商品" * 125,
                                "sku": "S" * 100,
                            }
                        )
                    }
                )
                for index in range(50)
            )
            return InventoryResult(items=items, metadata=metadata(rows=50, limit=50))

    class SerializedProvider(SequenceProvider):
        def estimate_input_tokens(self, request):
            estimates[request.mode] = estimator.estimate_input_tokens(request)
            return estimates[request.mode]

    provider = SerializedProvider(
        [
            usage_result(
                decision="tool",
                semantic_operation="inventory_list",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={"limit": 50},
            )
        ]
    )
    configure_operational_chat(db_session, business["id"], LargeSource(), provider)
    response = submit(
        api_client, user, business["id"], "Show current inventory", "large-result"
    )
    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "daily_ai_token_limit_reached"
    assert estimates["operational"] + 512 < 20000
    assert estimates["operational_synthesis"] + 512 + 12 > 20000
    assert len(provider.requests) == 1
    with migration_engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT total_tokens_used,tokens_reserved FROM business_ai_usage_daily"
            )
        ).one() == (12, 0)


def preference_choice_source():
    source = StubSource()
    source.locations = (
        source.locations[0],
        source.locations[1].model_copy(update={"label": "Test North Branch"}),
        source.locations[2].model_copy(update={"label": "Test North Warehouse"}),
    )
    return source


def test_reservation_sizing_admits_saved_preference_followup_budget(
    api_client, db_session, migration_engine
):
    user, business = active_business(api_client, db_session)
    source = preference_choice_source()
    holds = []

    class RecordedEstimateProvider(SequenceProvider):
        def estimate_input_tokens(self, request):
            # Saved live estimates: (4733 + 512) * 3 and (5954 + 512) * 3.
            return 5954 if request.pending_clarification else 4733

        def generate(self, request):
            with migration_engine.connect() as connection:
                hold = connection.execute(
                    text(
                        "SELECT reserved_tokens FROM ai_usage_reservations "
                        "WHERE status='reserved'"
                    )
                ).scalar_one()
                holds.append(hold)
                assert hold == self.estimate_input_tokens(request) + 512
            return super().generate(request)

    provider = RecordedEstimateProvider(
        [
            replace(
                usage_result(
                    decision="set_preference", location_reference="Test North"
                ),
                usage=TokenUsage(1388, 48, 1436, True),
            ),
            usage_result(
                decision="set_preference",
                pending_reply="unresolved",
                location_reference="Test North",
            ),
            usage_result(
                semantic_operation="conversation", pending_reply="selection", reply=""
            ),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    initial = submit(
        api_client,
        user,
        business["id"],
        "Use Test North for future inventory",
        "sizing-initial",
    )
    assert initial.status_code == 200, initial.text
    db_session.expire_all()
    pending = db_session.scalar(select(PendingOwnerOperationalPreference))
    original = (pending.id, pending.expires_at, pending.candidate_references)
    with migration_engine.connect() as connection:
        remaining = connection.scalar(
            text("SELECT 20000-total_tokens_used FROM business_ai_usage_daily")
        )
    assert remaining == 18564 < 19398
    followup = submit(api_client, user, business["id"], "eh", "sizing-followup")
    assert followup.status_code == 200, followup.text
    db_session.expire_all()
    pending = db_session.get(PendingOwnerOperationalPreference, original[0])
    assert (pending.id, pending.expires_at, pending.candidate_references) == original
    assert pending.state == "pending"
    assert db_session.scalar(select(UserOperationalPreference)) is None
    assert "Test North Branch" in followup.json()["assistant_message"]["content"]
    assert "Test North Warehouse" in followup.json()["assistant_message"]["content"]
    assert (
        submit(api_client, user, business["id"], "eh", "sizing-followup").status_code
        == 200
    )
    selected = submit(
        api_client, user, business["id"], "Test North Warehouse", "sizing-selection"
    )
    assert selected.status_code == 200, selected.text
    db_session.expire_all()
    assert (
        db_session.get(PendingOwnerOperationalPreference, original[0]).state
        == "completed"
    )
    assert (
        db_session.scalar(select(UserOperationalPreference)).location_type
        == "warehouse"
    )
    assert holds == [5245, 6466, 6466]
    with migration_engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT total_tokens_used,tokens_reserved FROM business_ai_usage_daily"
            )
        ).one() == (1460, 0)
        assert (
            connection.scalar(text("SELECT count(*) FROM business_ai_allowance_audit"))
            == 0
        )
        assert (
            connection.scalar(text("SELECT count(*) FROM owner_chat_rate_limit_events"))
            == 3
        )


@pytest.mark.parametrize(
    "route", ["category", "preference", "conversation", "inventory"]
)
@pytest.mark.parametrize("real_payload_estimate", [False, True])
def test_reservation_sizing_holds_each_actual_stage_payload(
    api_client, db_session, migration_engine, route, monkeypatch, real_payload_estimate
):
    user, business = active_business(api_client, db_session)
    monkeypatch.setattr(owner_chat, "_enqueue_summary_safely", lambda *args: None)
    source = StubSource()
    estimator = GeminiOwnerChatProvider(
        api_key="offline-unused", model="gemini-3.1-flash-lite", timeout_seconds=120
    )
    if route == "category":
        results = [
            usage_result(
                decision="tool",
                semantic_operation="inventory_category",
                entity_kind="category",
                entity_query="groceries",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={"category_filter": "groceries"},
            ),
            usage_result(
                category_resolution_status="matched",
                category_candidate_references=("category-1",),
            ),
            usage_result(reply="There are 8 units."),
        ]
    elif route == "preference":
        results = [
            usage_result(
                decision="set_preference", location_reference="my usual place"
            ),
            usage_result(
                preference_resolution_status="matched",
                preference_resolution_key="default_inventory_location",
                preference_location_candidate_references=("location_1",),
            ),
        ]
    elif route == "conversation":
        results = [
            usage_result(semantic_operation="conversation", reply=""),
            usage_result(reply="Here is some general advice."),
        ]
    else:
        results = [
            usage_result(
                decision="tool",
                semantic_operation="inventory_list",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={},
            ),
            usage_result(reply="There are 8 units."),
        ]

    class StageProvider(SequenceProvider):
        def estimate_input_tokens(self, request):
            if real_payload_estimate:
                return estimator.estimate_input_tokens(request)
            return {
                "operational": 100,
                "category_resolution": 900,
                "preference_resolution": 900,
                "conversation": 3000,
                "operational_synthesis": 5000,
            }[request.mode]

        def generate(self, request):
            with migration_engine.connect() as connection:
                held = connection.execute(
                    text(
                        "SELECT estimated_input_tokens,max_output_tokens,"
                        "reserved_tokens "
                        "FROM ai_usage_reservations WHERE status='reserved'"
                    )
                ).one()
                assert held.estimated_input_tokens == 10 * len(
                    self.requests
                ) + self.estimate_input_tokens(request)
                assert (
                    held.max_output_tokens
                    == 2 * len(self.requests) + request.max_output_tokens
                )
                assert (
                    connection.scalar(
                        text("SELECT tokens_reserved FROM business_ai_usage_daily")
                    )
                    == held.reserved_tokens
                )
            return super().generate(request)

    provider = StageProvider(results)
    configure_operational_chat(db_session, business["id"], source, provider)
    message = (
        "Some general advice please"
        if route == "conversation"
        else "Use my usual place for future inventory"
        if route == "preference"
        else "Show current inventory"
    )
    response = submit(api_client, user, business["id"], message, "stage-sizing")
    assert response.status_code == 200, response.text
    expected_modes = {
        "category": ["operational", "category_resolution", "operational_synthesis"],
        "preference": ["operational", "preference_resolution"],
        "conversation": ["operational", "conversation"],
        "inventory": ["operational", "operational_synthesis"],
    }
    assert [request.mode for request in provider.requests] == expected_modes[route]
    with migration_engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT total_tokens_used,tokens_reserved FROM business_ai_usage_daily"
            )
        ).one() == (12 * len(results), 0)
        assert (
            connection.scalar(text("SELECT count(*) FROM ai_usage_reservations")) == 1
        )


@pytest.mark.parametrize(
    "blocked_stage", ["operational", "category_resolution", "operational_synthesis"]
)
def test_reservation_sizing_rejects_unaffordable_stage_before_dispatch(
    api_client, db_session, migration_engine, blocked_stage
):
    user, business = active_business(api_client, db_session)

    class ExpensiveStageProvider(SequenceProvider):
        def estimate_input_tokens(self, request):
            return 20000 if request.mode == blocked_stage else 100

    provider = ExpensiveStageProvider(
        [
            usage_result(
                decision="tool",
                semantic_operation="inventory_category",
                entity_kind="category",
                entity_query="groceries",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={"category_filter": "groceries"},
            ),
            usage_result(
                category_resolution_status="matched",
                category_candidate_references=("category-1",),
            ),
        ]
    )
    configure_operational_chat(db_session, business["id"], StubSource(), provider)
    response = submit(
        api_client, user, business["id"], "Show current inventory", "stage-blocked"
    )
    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "daily_ai_token_limit_reached"
    assert "Retry-After" in response.headers
    expected_calls = [
        "operational",
        "category_resolution",
        "operational_synthesis",
    ].index(blocked_stage)
    assert len(provider.requests) == expected_calls
    assert (
        submit(
            api_client, user, business["id"], "Show current inventory", "stage-blocked"
        ).status_code
        == 409
    )
    assert len(provider.requests) == expected_calls
    with migration_engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT coalesce(sum(tokens_reserved),0) "
                    "FROM business_ai_usage_daily"
                )
            )
            == 0
        )
        assert (
            connection.scalar(
                text(
                    "SELECT coalesce(sum(total_tokens_used),0) "
                    "FROM business_ai_usage_daily"
                )
            )
            == 12 * expected_calls
        )
        assert connection.scalar(
            text("SELECT count(*) FROM owner_chat_rate_limit_events")
        ) == bool(expected_calls)
        assert connection.scalar(
            text("SELECT count(*) FROM ai_usage_reservations")
        ) == bool(expected_calls)


@pytest.mark.parametrize(
    "response_kind",
    [
        "saved_response",
        "unresolved",
        "unresolved_casual",
        "confirmation",
        "missing_classification",
        "malformed_known",
        "malformed_unknown",
        "invalid_quote",
        "conflicting_cancel",
    ],
)
def test_pending_preference_recovers_across_requests_without_renewing_expiry(
    api_client, db_session, migration_engine, response_kind
):
    user, business = active_business(api_client, db_session)
    source = preference_choice_source()

    class PendingReplyProvider(SequenceProvider):
        def generate(self, request):
            if len(self.requests) != 1:
                return super().generate(request)
            self.requests.append(request)
            if response_kind == "saved_response":
                # Exact failed planner response from final-validation call 9.
                raw = (
                    '{"semantic_operation":"conversation","decision":"final",'
                    '"pending_reply":"unrelated"}'
                )
                replay = GeminiOwnerChatProvider(
                    api_key="offline-unused",
                    model="gemini-3.1-flash-lite",
                    timeout_seconds=120,
                    transport=httpx.MockTransport(
                        lambda _: httpx.Response(
                            200,
                            json={
                                "candidates": [
                                    {
                                        "finishReason": "STOP",
                                        "content": {"parts": [{"text": raw}]},
                                    }
                                ],
                                "usageMetadata": {
                                    "promptTokenCount": 1491,
                                    "candidatesTokenCount": 32,
                                    "totalTokenCount": 1523,
                                },
                            },
                        )
                    ),
                )
                return replay.generate(request)
            if response_kind.startswith("malformed"):
                raise OwnerChatProviderInvalidResponse(
                    reason="schema_validation_failed",
                    usage=usage_result().usage
                    if response_kind == "malformed_known"
                    else None,
                )
            if response_kind == "invalid_quote":
                return usage_result(
                    semantic_operation="conversation",
                    pending_reply="unrelated",
                    pending_request="Tell me a joke",
                    reply="",
                )
            if response_kind == "conflicting_cancel":
                return usage_result(
                    decision="clear_preference",
                    pending_reply="cancel",
                    pending_request="eh",
                )
            return usage_result(
                decision="set_preference",
                location_reference="Test North Branch",
                pending_reply=None
                if response_kind == "missing_classification"
                else "unresolved"
                if response_kind == "unresolved_casual"
                else response_kind,
            )

    provider = PendingReplyProvider(
        [
            usage_result(decision="set_preference", location_reference="Test North"),
            usage_result(),  # The second call is supplied above.
            usage_result(
                reply="", semantic_operation="conversation", pending_reply="selection"
            ),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    initial = submit(
        api_client,
        user,
        business["id"],
        "Use Test North for future inventory",
        "initial",
    )
    assert initial.status_code == 200, initial.text
    db_session.expire_all()
    pending = db_session.scalar(select(PendingOwnerOperationalPreference))
    original = (pending.id, pending.expires_at, pending.candidate_references)
    origin = pending.originating_message_id
    message = "hello" if response_kind == "unresolved_casual" else "eh"
    reply = submit(api_client, user, business["id"], message, "unresolved")
    assert reply.status_code == (
        503 if response_kind == "malformed_unknown" else 200
    ), reply.text
    db_session.expire_all()
    pending = db_session.get(PendingOwnerOperationalPreference, original[0])
    assert pending.state == "pending"
    assert (pending.id, pending.expires_at, pending.candidate_references) == original
    assert db_session.scalar(select(UserOperationalPreference)) is None
    if response_kind == "malformed_unknown":
        assert pending.originating_message_id == origin
    else:
        assert pending.originating_message_id == uuid.UUID(
            reply.json()["owner_message"]["id"]
        )
        assert "Test North Branch" in reply.json()["assistant_message"]["content"]
        assert "Test North Warehouse" in reply.json()["assistant_message"]["content"]
    assert provider.requests[1].pending_clarification["operation"] == "preference"
    assert len(provider.requests) == 2 and source.calls == []
    replay = submit(api_client, user, business["id"], message, "unresolved")
    assert replay.status_code == (409 if response_kind == "malformed_unknown" else 200)
    assert len(provider.requests) == 2
    chosen = submit(api_client, user, business["id"], "Test North Warehouse", "chosen")
    assert chosen.status_code == 200, chosen.text
    db_session.expire_all()
    pending = db_session.get(PendingOwnerOperationalPreference, original[0])
    assert pending.state == "completed" and pending.expires_at == original[1]
    assert pending.candidate_references == original[2]
    saved = db_session.scalar(select(UserOperationalPreference))
    assert saved.location_external_id == source.locations[2].external_location_id
    assert saved.location_type == "warehouse"
    assert len(provider.requests) == 3 and source.calls == []
    assert (
        provider.requests[2].pending_clarification["candidates"]
        == provider.requests[1].pending_clarification["candidates"]
    )
    with migration_engine.connect() as connection:
        ledger = connection.execute(
            text(
                "SELECT total_tokens,reserved_tokens,counts_authoritative,status "
                "FROM ai_usage_reservations ORDER BY created_at,id"
            )
        ).all()
    assert len(ledger) == 3
    assert ledger[0].total_tokens == ledger[2].total_tokens == 12
    if response_kind == "malformed_unknown":
        assert ledger[1].total_tokens == ledger[1].reserved_tokens
        assert not ledger[1].counts_authoritative and ledger[1].status == "charged"
    else:
        assert ledger[1].total_tokens == (
            1523 if response_kind == "saved_response" else 12
        )
        assert ledger[1].counts_authoritative and ledger[1].status == "completed"


@pytest.mark.parametrize(
    "transition", ["cancel", "replace", "clear", "unrelated", "conversation"]
)
def test_pending_preference_explicit_transitions_persist_without_candidate_leakage(
    api_client, db_session, transition
):
    user, business = active_business(api_client, db_session)
    source = preference_choice_source()
    message = {
        "cancel": "Cancel that choice",
        "replace": "Instead use Jbeil Branch by default",
        "clear": "Remove my saved default inventory location",
        "unrelated": "Show inventory at Test North Warehouse",
        "conversation": "Tell me a joke",
    }[transition]
    result = (
        usage_result(
            decision="set_preference",
            location_reference="Jbeil Branch",
            pending_reply="replace",
            pending_request=message,
        )
        if transition == "replace"
        else usage_result(
            decision="clear_preference",
            pending_reply="replace",
            pending_request=message,
        )
        if transition == "clear"
        else usage_result(
            decision="tool",
            semantic_operation="inventory_list",
            tool_name=CURRENT_INVENTORY_TOOL,
            tool_arguments={"location_reference": "Test North Warehouse"},
            pending_reply="unrelated",
            pending_request=message,
        )
        if transition == "unrelated"
        else usage_result(
            reply="",
            semantic_operation="conversation",
            pending_reply="cancel" if transition == "cancel" else "unrelated",
            pending_request=message,
        )
    )
    results = [
        usage_result(decision="set_preference", location_reference="Test North"),
        result,
    ]
    if transition in {"unrelated", "conversation"}:
        results.append(
            usage_result(
                reply="Here is 8 from the requested result.",
                semantic_operation=None
                if transition == "conversation"
                else "unsupported",
            )
        )
    results.append(usage_result(reply="Please provide a new request."))
    provider = SequenceProvider(results)
    configure_operational_chat(db_session, business["id"], source, provider)
    config = db_session.scalar(select(OperationalDataSourceConfig))
    db_session.add(
        UserOperationalPreference(
            user_id=user.id,
            business_id=uuid.UUID(business["id"]),
            source_id=config.id,
            preference_key="default_inventory_location",
            location_type="branch",
            location_external_id="BR-JBEIL",
        )
    )
    db_session.commit()
    initial = submit(
        api_client,
        user,
        business["id"],
        "Use Test North for future inventory",
        "initial",
    )
    assert initial.status_code == 200, initial.text
    pending = db_session.scalar(select(PendingOwnerOperationalPreference))
    identifier, expires = pending.id, pending.expires_at
    changed = submit(api_client, user, business["id"], message, "changed")
    assert changed.status_code == 200, changed.text
    db_session.expire_all()
    pending = db_session.get(PendingOwnerOperationalPreference, identifier)
    assert pending.state == "superseded" and pending.expires_at == expires
    saved = db_session.scalar(select(UserOperationalPreference))
    if transition == "clear":
        assert saved is None
    else:
        assert saved.location_external_id == "BR-JBEIL"
    assert source.calls == (
        [CURRENT_INVENTORY_TOOL] if transition == "unrelated" else []
    )
    later = submit(api_client, user, business["id"], "Test North Branch", "later")
    assert later.status_code == 200, later.text
    assert provider.requests[-1].pending_clarification is None
    db_session.expire_all()
    assert (
        db_session.get(PendingOwnerOperationalPreference, identifier).state
        == "superseded"
    )


@pytest.mark.parametrize(
    "invalidated", ["expired", "source_changed", "candidate_removed"]
)
def test_pending_preference_reconstruction_revalidates_expiry_and_source(
    api_client, db_session, migration_engine, monkeypatch, invalidated
):
    user, business = active_business(api_client, db_session)
    source = preference_choice_source()
    provider = SequenceProvider(
        [
            usage_result(decision="set_preference", location_reference="Test North"),
            usage_result(reply="Please clarify the request."),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    assert (
        submit(
            api_client,
            user,
            business["id"],
            "Use Test North for future inventory",
            "initial",
        ).status_code
        == 200
    )
    pending = db_session.scalar(select(PendingOwnerOperationalPreference))
    identifier, expires = pending.id, pending.expires_at
    if invalidated == "expired":
        monkeypatch.setattr(
            owner_chat, "utc_now", lambda: expires + timedelta(seconds=1)
        )
    elif invalidated == "source_changed":
        config = db_session.scalar(select(OperationalDataSourceConfig))
        with migration_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE operational_data_sources SET updated_at=:time WHERE id=:id"
                ),
                {"time": datetime.now(UTC) + timedelta(seconds=1), "id": config.id},
            )
        db_session.expire_all()
    else:
        source.locations = source.locations[:-1]
    response = submit(api_client, user, business["id"], "Test North Branch", "later")
    assert response.status_code == 200, response.text
    db_session.expire_all()
    pending = db_session.get(PendingOwnerOperationalPreference, identifier)
    assert pending.state == ("expired" if invalidated == "expired" else "invalidated")
    assert pending.expires_at == expires
    assert db_session.scalar(select(UserOperationalPreference)) is None
    assert provider.requests[-1].pending_clarification is None
    assert source.calls == []


def test_pending_preference_yes_cannot_save_a_guessed_candidate(api_client, db_session):
    user, business = active_business(api_client, db_session)
    source = StubSource()
    source.locations = tuple(
        candidate.model_copy(update={"label": f"Test North {suffix}"})
        for candidate, suffix in zip(
            source.locations[:2], ("Branch", "Warehouse"), strict=True
        )
    )
    provider = SequenceProvider(
        [
            usage_result(decision="set_preference", location_reference="Test North"),
            usage_result(
                decision="set_preference",
                location_reference="Test North Branch",
                pending_reply="confirmation",
            ),
            usage_result(
                reply="", semantic_operation="conversation", pending_reply="selection"
            ),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    first = submit(
        api_client,
        user,
        business["id"],
        "Use Test North for future inventory",
        "preference-choice",
    )
    assert first.status_code == 200, first.text
    pending = db_session.scalar(select(PendingOwnerOperationalPreference))
    expires = pending.expires_at
    yes = submit(api_client, user, business["id"], "yes", "preference-yes")
    assert yes.status_code == 200, yes.text
    assert db_session.scalar(select(UserOperationalPreference)) is None
    db_session.refresh(pending)
    assert pending.state == "pending" and pending.expires_at == expires
    selected = submit(
        api_client, user, business["id"], "Test North Branch", "preference-selected"
    )
    assert selected.status_code == 200, selected.text
    assert (
        db_session.scalar(select(UserOperationalPreference)).location_external_id
        == source.locations[0].external_location_id
    )


@pytest.mark.parametrize(
    "message",
    [
        "Hello, show current inventory in Jbeil.",
        "مرحبا، اعرض المخزون الحالي في جبيل.",
        "kifak, shu el stock bi Jbeil?",
    ],
)
def test_mixed_greeting_business_request_keeps_the_ai_operational_plan(
    api_client, db_session, message
):
    user, business = active_business(api_client, db_session)
    source = StubSource()
    provider = SequenceProvider(
        [
            usage_result(
                decision="tool",
                semantic_operation="inventory_list",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={"location_reference": "Jbeil Branch", "limit": 5},
            ),
            usage_result(reply="The validated inventory result is ready."),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    response = submit(api_client, user, business["id"], message, "mixed-request")
    assert response.status_code == 200, response.text
    assert [request.mode for request in provider.requests] == [
        "operational",
        "operational_synthesis",
    ]
    assert provider.requests[0].messages[-1].content == message
    assert source.calls == [CURRENT_INVENTORY_TOOL]
    assert source.last_inventory_query.branch_external_id == "BR-JBEIL"


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


@pytest.mark.parametrize("proposal", ["Beirut Branch", "BR-1"])
def test_inventory_model_expansion_cannot_choose_an_ambiguous_source_location(
    api_client, db_session, proposal
):
    user, business = active_business(api_client, db_session)
    source = StubSource()
    source.locations = tuple(
        candidate.model_copy(update={"label": f"Beirut {suffix}"})
        for candidate, suffix in zip(
            source.locations[:2], ("Branch", "Warehouse"), strict=True
        )
    )
    provider = SequenceProvider(
        [
            usage_result(
                decision="tool",
                semantic_operation="inventory_list",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={"location_reference": proposal},
            ),
            usage_result(reply="Which location: Beirut Branch or Beirut Warehouse?"),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    response = submit(
        api_client, user, business["id"], "Show inventory at Beirut", "location"
    )
    assert response.status_code == 200, response.text
    assert source.calls == []
    output = provider.requests[-1].tool_results[0].output
    assert output["status"] == "ambiguous"
    assert {item["label"] for item in output["candidates"]} == {
        "Beirut Branch",
        "Beirut Warehouse",
    }


def test_explicit_inventory_source_location_survives_a_model_expansion(
    api_client, db_session
):
    user, business = active_business(api_client, db_session)
    source = StubSource()
    provider = SequenceProvider(
        [
            usage_result(
                decision="tool",
                semantic_operation="inventory_list",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={"location_reference": source.locations[1].label},
            ),
            usage_result(reply="Validated stock: 12 available."),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    response = submit(
        api_client,
        user,
        business["id"],
        f"Show inventory at {source.locations[0].label}",
        "explicit-location",
    )
    assert response.status_code == 200, response.text
    assert (
        source.last_inventory_query.branch_external_id
        == source.locations[0].external_location_id
    )


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
    run_loop = owner_chat._run_operational_loop

    def run_at_reporting_date(*args, **kwargs):
        # Freeze reporting time, not the claim lease checked by PostgreSQL's clock.
        with monkeypatch.context() as reporting_clock:
            reporting_clock.setattr(owner_chat, "utc_now", lambda: requested_at)
            return run_loop(*args, **kwargs)

    monkeypatch.setattr(owner_chat, "_run_operational_loop", run_at_reporting_date)
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
            usage_result(reply="Clarify", pending_reply="selection"),
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
            usage_result(
                decision="set_preference",
                location_reference="Jbeil",
                pending_reply="replace",
                pending_request="instead use Jbeil branch by default",
            ),
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
