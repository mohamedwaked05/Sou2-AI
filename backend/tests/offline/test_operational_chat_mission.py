"""Controlled planner HTTP -> real executor -> real SQL -> shared presentation.

Platform persistence/accounting use the existing state-session fixture; source
queries execute on memory-only SQLite. Native database ACLs have separate tests.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest
from app.agent.owner_chat_provider import ProviderToolDefinition
from app.database.models import SourceOperationalMappingRevision, ToolCallLog
from app.services import operational_mapping, source_mapping
from sqlalchemy import Engine
from test_catalogue_chat import offline_provider
from test_mapped_operations import NOW, fixture_source
from test_owner_chat_state import new_turn, run
from test_owner_chat_state import system as system

CONNECT = Engine.connect


@pytest.fixture
def mapped_chat(system, monkeypatch):
    def memory_only(engine, *args, **kwargs):
        assert engine.url.database == ":memory:" and engine.dialect.name == "sqlite"
        return CONNECT(engine, *args, **kwargs)

    monkeypatch.setattr(Engine, "connect", memory_only)
    source = fixture_source(78)
    source.adapter.mapping = source.mapping.model_copy(
        update={"business_id": system.business_id, "source_id": system.source.id}
    )
    system.session.add(
        SourceOperationalMappingRevision(
            id=__import__("uuid").uuid4(),
            business_id=system.business_id,
            source_id=system.source.id,
            version=1,
            mapping=source.adapter.mapping.model_dump(mode="json"),
            discovery=source.discovery.model_dump(mode="json"),
        )
    )
    from app.tools import operational
    from app.tools.catalogue import execute_product_search

    class FixtureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    # Current-price queries must use the same clock as the dated source fixture.
    monkeypatch.setattr(operational, "datetime", FixtureClock)

    monkeypatch.setattr(operational, "execute_product_search", execute_product_search)
    monkeypatch.setattr(
        operational_mapping, "mapped_source", lambda *_: source.catalogue
    )
    monkeypatch.setattr(source_mapping, "mapped_source", lambda *_: source.catalogue)
    monkeypatch.setattr(
        source_mapping,
        "latest_approved",
        lambda *_: SimpleNamespace(
            version=1, schema_fingerprint=source.discovery.schema_fingerprint
        ),
    )
    system.definitions = tuple(
        ProviderToolDefinition(**system.executor.registry[name].provider_schema())
        for name in ("product_search", "operational_report")
    )
    yield system, source
    source.engine.dispose()


@pytest.mark.parametrize(
    "capability",
    [
        "stock",
        "availability",
        "prices",
        "sales",
        "revenue",
        "refunds",
        "best_sellers",
        "restocking",
    ],
)
@pytest.mark.parametrize("language", ["english", "arabic", "arabizi"])
def test_typed_planner_dispatches_actual_source_and_deterministic_reply(
    mapped_chat, capability, language
):
    system, source = mapped_chat
    questions = {
        "stock": (
            "How much Diet Random 7UP is in stock?",
            "قديش مخزون Diet Random 7UP؟",
            "adde stock Diet Random 7UP?",
        ),
        "availability": (
            "Is Diet Random 7UP available?",
            "في Diet Random 7UP بالمخزون؟",
            "fi Diet Random 7UP bel stock?",
        ),
        "prices": (
            "What is the selling price of Diet Random 7UP?",
            "قديش سعر Diet Random 7UP؟",
            "adde se3er Diet Random 7UP?",
        ),
        "sales": (
            "What were our sales yesterday?",
            "قديش مبيعات مبارح؟",
            "adde mabi3at mbere7?",
        ),
        "revenue": (
            "What was our net revenue yesterday?",
            "قديش صافي الإيرادات مبارح؟",
            "adde net revenue mbere7?",
        ),
        "refunds": (
            "How much was refunded yesterday?",
            "قديش رجعنا مصاري مبارح؟",
            "adde raja3na masare mbere7?",
        ),
        "best_sellers": (
            "What are our top-selling products?",
            "شو أكثر المنتجات مبيعاً؟",
            "shu aktar products mabi3an?",
        ),
        "restocking": (
            "What needs restocking?",
            "شو لازم نعبي بالمخزون؟",
            "shu baddo ta3biye?",
        ),
    }
    message = questions[capability][("english", "arabic", "arabizi").index(language)]
    new_turn(system, message)
    args = dict(capability=capability, product_query="Diet Random 7UP")
    if capability in {"sales", "revenue", "refunds", "best_sellers"}:
        args.update(start="2026-10-15T00:00:00Z", end="2026-10-16T00:00:00Z")
    provider, calls = offline_provider(
        dict(
            decision="tool",
            semantic_operation="operational_report",
            tool_name="operational_report",
            arguments=args,
        )
    )
    result, usage = run(system, provider)
    assert len(calls) == 1 and usage.total_tokens == 180
    assert result.operational_presentation.result.capability == capability
    assert result.operational_presentation.result.status == "ok"
    assert result.operational_presentation.result.source_id == system.source.id
    assert result.operational_presentation.result.rows[0].unit == "kg"
    assert "ID:" in result.reply
    audit = [r for r in system.session.added if isinstance(r, ToolCallLog)]
    assert (
        audit[-1].tool_name == "operational_report"
        and audit[-1].status.value == "success"
    )
    assert "Random" not in audit[-1].args_hash


def test_planner_cannot_supply_authority_ids(mapped_chat):
    from app.schemas.operational_mapping import OperationalPlannerRead
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        OperationalPlannerRead(
            capability="stock", product_id="other-business", catalogue_revision=1
        )


def test_missing_selected_context_does_not_turn_into_unscoped_query(mapped_chat):
    system, _source = mapped_chat
    new_turn(system, "Show its stock")
    provider, _ = offline_provider(
        dict(
            decision="tool",
            semantic_operation="operational_report",
            tool_name="operational_report",
            arguments=dict(capability="stock", reference="selected"),
        )
    )
    result, _ = run(system, provider)
    assert result.operational_presentation is None
    assert result.decision == "unavailable"


def test_operational_clarification_resumes_only_after_fresh_exact_selection(
    mapped_chat,
):
    system, source = mapped_chat
    with source.engine.begin() as connection:
        connection.execute(
            source.tables[0].insert(),
            dict(tenant="A", code="002", label="Random Diet 7UP Large"),
        )
        connection.execute(
            source.tables[1].insert(),
            dict(
                id=3,
                tenant="A",
                product="002",
                location="east",
                unit="kg",
                balance=5,
                reserved=1,
                threshold=2,
                target=10,
            ),
        )
    new_turn(system, "Stock of Random?")
    provider, _ = offline_provider(
        dict(
            decision="tool",
            semantic_operation="operational_report",
            tool_name="operational_report",
            arguments=dict(capability="stock", product_query="Random"),
        )
    )
    result, _ = run(system, provider)
    assert result.operational_presentation.result.status == "ambiguous"
    assert (
        system.owner.operational_clarification["deferred_operation"]["capability"]
        == "stock"
    )
    new_turn(system, "Random Diet 7UP Large")
    provider, calls = offline_provider()
    selected, usage = run(system, provider)
    assert calls == [] and usage.total_tokens == 0
    assert selected.operational_presentation.result.capability == "stock"
    assert selected.operational_presentation.result.rows[0].on_hand == 5
    assert selected.operational_presentation.result.rows[0].location == "east"


def test_selected_product_survives_sales_followup_without_new_identity(mapped_chat):
    system, source = mapped_chat
    from test_owner_chat_state import presentation, product_plan

    new_turn(system, "Random Diet 7UP")
    provider, _ = offline_provider(
        product_plan("Random Diet 7UP", product_presentation=presentation("details"))
    )
    selected, _ = run(system, provider)
    identifier = selected.operational_presentation.result.items[0].external_product_id
    new_turn(system, "How many of it sold yesterday?")
    provider, _ = offline_provider(
        dict(
            decision="tool",
            semantic_operation="operational_report",
            tool_name="operational_report",
            arguments=dict(
                capability="sales",
                reference="selected",
                start="2026-10-15T00:00:00Z",
                end="2026-10-16T00:00:00Z",
            ),
        )
    )
    result, _ = run(system, provider)
    assert result.operational_presentation.result.rows[0].product_id == identifier
    assert result.operational_presentation.result.rows[0].gross_quantity == 2


def test_reporting_timezone_comes_from_approved_source_not_business(mapped_chat):
    system, _source = mapped_chat
    revision = next(
        row
        for row in system.session.added
        if isinstance(row, SourceOperationalMappingRevision)
    )
    revision.mapping = {**revision.mapping, "timezone": "Pacific/Honolulu"}
    timezone, metrics = system.executor.sales_reporting_context(
        system.user, system.business_id
    )
    assert timezone == "Pacific/Honolulu"
    assert set(metrics) == {"revenue", "sales_count"}


def test_explicit_numeric_product_request_cannot_reuse_selected_identity(mapped_chat):
    from test_owner_chat_state import presentation, product_plan

    system, _ = mapped_chat
    new_turn(system, "Random Diet 7UP")
    provider, _ = offline_provider(
        product_plan("Random Diet 7UP", product_presentation=presentation("details"))
    )
    run(system, provider)
    new_turn(system, "Stock for product ID 0002")
    provider, _ = offline_provider(
        dict(
            decision="tool",
            semantic_operation="operational_report",
            tool_name="operational_report",
            arguments=dict(capability="stock", reference="selected"),
        )
    )
    result, _ = run(system, provider)
    assert result.operational_presentation is None
    assert result.decision == "unavailable"


def test_operational_snapshot_must_match_current_catalogue_approval(mapped_chat):
    from app.integrations.discovery import SourceMappingError

    system, source = mapped_chat
    revision = next(
        row
        for row in system.session.added
        if isinstance(row, SourceOperationalMappingRevision)
    )
    payload = source.discovery.model_dump(mode="json")
    payload["objects"][0]["validation_stamp"] = "f" * 64
    revision.discovery = payload
    with pytest.raises(SourceMappingError, match="mapping_schema_changed"):
        operational_mapping.operational_source(
            system.session, system.executor._profiles, system.source
        )


@pytest.mark.parametrize("mapped", [True, False])
def test_planner_price_policy_matches_registered_capabilities(mapped_chat, mapped):
    from app.agent.owner_chat_provider import (
        OwnerChatRequest,
        _operational_instructions,
    )
    from test_catalogue_chat import NOW, PROFILE

    system, _source = mapped_chat
    tools = system.definitions if mapped else ()
    request = OwnerChatRequest(
        profile=PROFILE,
        knowledge=(),
        messages=(),
        requested_at=NOW,
        tools=tools,
        mode="operational",
    )
    instructions = _operational_instructions(request).split("Safe context follows:")[0]
    if mapped:
        assert "arguments.capability=prices" in instructions
        assert "must use product_price" not in instructions
        assert "legacy current_inventory tool, never operational_report" in instructions
    else:
        assert "must use product_price" in instructions
