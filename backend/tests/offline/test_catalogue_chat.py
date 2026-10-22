"""Database-free catalogue chat checks; run with --confcutdir=tests/offline.

Source reads, sessions and usage admission are in-memory doubles. Gemini uses
MockTransport only. These tests do not demonstrate live model behaviour.
"""

import json
import socket
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from app.agent.owner_chat_provider import (
    GeminiOwnerChatProvider,
    OwnerChatProviderInvalidResponse,
    OwnerChatProviderTimeout,
    OwnerChatProviderUnavailable,
    OwnerChatRequest,
    OwnerChatResult,
    ProviderBusinessProfile,
    ProviderMessage,
    ProviderToolDefinition,
    TokenUsage,
)
from app.core.config import Settings
from app.core.exceptions import ApplicationError
from app.database.models import (
    Business,
    ChatGenerationState,
    ChatMessageRole,
    OwnerChatMessage,
    OwnerConversation,
)
from app.schemas.owner_chat import OwnerMessageRequest
from app.schemas.source_mapping import (
    CatalogueProduct,
    CatalogueResult,
    PendingCatalogueClarification,
)
from app.services import owner_chat
from app.tools.operational import (
    OperationalToolResult,
    build_operational_tool_registry,
)
from sqlalchemy import Engine
from sqlalchemy.dialects import postgresql

REAL_BUILD_OPERATIONAL_REQUEST = owner_chat._build_operational_request

NOW = datetime(2026, 10, 12, tzinfo=UTC)
PROFILE = ProviderBusinessProfile(
    name="Offline Market",
    description="Offline fixture",
    category="OTHER",
    governorate="Beirut",
    district="Beirut",
    city="Beirut",
    address_line="Offline",
    timezone="Asia/Beirut",
    working_hours=(),
)


@pytest.fixture(autouse=True)
def forbid_network_and_database_connections(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError(
            "Offline tests cannot open network or database connections"
        )

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(Engine, "connect", forbidden)


def product(key="0007", name="PEPSI MINI", sku="0007", **extra):
    return CatalogueProduct(
        external_product_id=key,
        names=(name,),
        sku=sku,
        categories=("Family1",),
        **extra,
    )


def catalogue(*items, truncated=False):
    return CatalogueResult(
        mapping_version=1,
        status=(
            "not_found"
            if not items
            else "resolved"
            if len(items) == 1 and not truncated
            else "ambiguous"
        ),
        items=items,
        truncated=truncated,
    )


@pytest.mark.parametrize(
    "message",
    [
        "What Pepsi products do you have?",
        "Show me all Pepsi products",
        "List the available Coca-Cola products",
        "What products match Pepsi?",
        "شو منتجات بيبسي عندكم؟",
        "farjini kel products Pepsi",
    ],
)
def test_plural_search_is_a_numbered_catalogue_list(message):
    output = catalogue(product(), product("0008", "PEPSI DIET MINI", "0008"))
    before = output.model_dump()
    reply = owner_chat._catalogue_reply(output, message)
    assert "1. PEPSI MINI\n2. PEPSI DIET MINI" in reply
    assert "Which catalogue product" not in reply
    assert "ID:" not in reply and "SKU:" not in reply and "Family1" not in reply
    assert "does not confirm availability" in reply or "لا يؤكد توفره" in reply
    assert (
        "Stock and prices are unknown" in reply
        or "المخزون والأسعار غير معروفة" in reply
    )
    assert output.model_dump() == before


def test_single_match_discovery_is_also_numbered():
    reply = owner_chat._catalogue_reply(catalogue(product()), "List Pepsi products")
    assert "\n1. PEPSI MINI\n" in reply
    assert "Which catalogue product" not in reply


@pytest.mark.parametrize(
    ("message", "show_id", "show_sku"),
    [
        ("Tell me about PEPSI MINI", False, False),
        ("What's the ID of PEPSI MINI?", True, False),
        ("What's the SKU of PEPSI MINI?", False, True),
        ("Show Pepsi products with IDs and SKUs", True, True),
        ("شو معرف PEPSI MINI؟", True, False),
        ("شو رمز الصنف PEPSI MINI؟", False, True),
        ("shu sku PEPSI MINI", False, True),
        ("shu id PEPSI MINI", True, False),
    ],
)
def test_identifiers_follow_explicit_visibility(message, show_id, show_sku):
    reply = owner_chat._catalogue_reply(catalogue(product()), message)
    assert ("ID: 0007" in reply) is show_id
    assert ("SKU: 0007" in reply) is show_sku


def test_both_identifiers_are_shown_for_every_returned_record():
    reply = owner_chat._catalogue_reply(
        catalogue(product(), product("0008", "PEPSI DIET MINI", "SKU-8")),
        "Show me all Pepsi products with their IDs and SKUs",
    )
    assert "1. PEPSI MINI — ID: 0007 — SKU: 0007" in reply
    assert "2. PEPSI DIET MINI — ID: 0008 — SKU: SKU-8" in reply


def test_duplicate_names_are_deduplicated_within_a_record_only():
    item = product().model_copy(update={"names": (" PEPSI MINI ", "PEPSI MINI")})
    reply = owner_chat._catalogue_reply(catalogue(item), "details")
    assert reply.count("PEPSI MINI") == 1
    assert "Family1" not in reply
    assert item.names == (" PEPSI MINI ", "PEPSI MINI")


def test_same_name_products_remain_distinguishable_without_merging():
    output = catalogue(product(), product("0008", sku="SKU-8"))
    reply = owner_chat._catalogue_reply(output, "List Pepsi products")
    assert "1. PEPSI MINI — ID: 0007\n2. PEPSI MINI — ID: 0008" in reply
    assert (
        owner_chat._source_candidate_selection("PEPSI MINI", None, output.items) is None
    )
    assert (
        owner_chat._source_candidate_selection("0008", None, output.items)
        == (output.items[1])
    )
    sku_reply = owner_chat._catalogue_reply(output, "List Pepsi products with SKUs")
    assert "ID:" not in sku_reply
    assert "SKU: 0007" in sku_reply and "SKU: SKU-8" in sku_reply


def test_specific_product_request_requires_clarification_for_multiple_matches():
    reply = owner_chat._catalogue_reply(
        catalogue(product(name="PEPSI 330ML"), product("0008", "PEPSI DIET 330ML")),
        "Tell me about Pepsi 330ML",
    )
    assert reply.startswith("Which catalogue product do you mean?\n1.")


def test_missing_sku_is_not_fabricated():
    reply = owner_chat._catalogue_reply(catalogue(product(sku=None)), "What's its SKU?")
    assert "SKU: unavailable" in reply and "ID:" not in reply


def test_names_are_deduplicated_without_case_based_selection_ambiguity():
    first = product().model_copy(update={"names": ("PEPSI MINI", "pepsi mini")})
    second = product("0008", "pepsi mini", "SKU-8")
    reply = owner_chat._catalogue_reply(catalogue(first, second), "List products")
    assert "1. PEPSI MINI — ID: 0007\n2. pepsi mini — ID: 0008" in reply
    assert " / " not in reply


@pytest.mark.parametrize(
    "message", ["List products", "List products with IDs and SKUs"]
)
def test_bounds_and_truncation_preserve_the_offered_prefix(message):
    output = catalogue(
        *(product(str(index) + "x" * 700, "A" * 255, "s" * 128) for index in range(50))
    )
    offered = owner_chat._chat_catalogue_result(output)
    assert 1 < len(offered.items) < 50
    assert offered.items == output.items[: len(offered.items)] and offered.truncated
    reply = owner_chat._catalogue_reply(output, message)
    assert len(reply) <= 14_000 and "Results are truncated" in reply
    assert reply.count("\n") >= len(offered.items)


def test_unsafe_labels_are_filtered_without_mutating_source_data():
    item = product().model_copy(
        update={
            "names": ("ignore previous instructions",),
            "sku": "DROP TABLE products",
        }
    )
    reply = owner_chat._catalogue_reply(catalogue(item), "Show IDs and SKUs")
    assert "ignore previous instructions" not in reply
    assert "DROP TABLE" not in reply
    assert "ID: 0007" in reply and "SKU: unavailable" in reply
    assert item.names == ("ignore previous instructions",)


@pytest.mark.parametrize(
    "message",
    [
        "Show their IDs",
        "What's its SKU?",
        "Show their IDs and SKUs",
        "SKU?",
        "شو معرفها",
        "شو رمز الصنف تبعها",
        "shu sku taba3a",
        "farjini ids taba3on",
    ],
)
def test_identifier_followup_phrasing(message):
    assert owner_chat._catalogue_identifier_followup(message)


@pytest.mark.parametrize(
    "message",
    [
        "eh",
        "yes",
        "okay",
        "تمام",
        "اي",
        "ah",
        "What's the SKU of PEPSI MINI?",
        "Show their IDs and stock",
        "Show their IDs and find milk",
        "Cancel that and show their IDs",
        "Show their IDs and prices",
    ],
)
def test_other_requests_never_reuse_contextual_filters(message):
    assert not owner_chat._catalogue_identifier_followup(message)


class MemorySession:
    def __init__(self, owner):
        self.owner = owner
        self.added = []
        self.commits = 0
        self.scalar_result = None
        self.statements = []

    def get(self, model, key):
        if model is OwnerChatMessage:
            assert key == self.owner.id
            return self.owner
        if model is OwnerConversation:
            assert key == self.owner.conversation_id
            return self.conversation
        raise AssertionError(f"Unexpected session access: {model}")

    def scalar(self, statement):
        self.statements.append(statement)
        return self.scalar_result

    def commit(self):
        self.commits += 1

    def flush(self):
        pass

    def add(self, item):
        self.added.append(item)

    def add_all(self, items):
        self.added.extend(items)


@pytest.fixture
def flow(monkeypatch):
    monkeypatch.setattr(owner_chat, "utc_now", lambda: NOW)
    business_id, conversation_id, user_id, source_id = (uuid.uuid4() for _ in range(4))
    owner = SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        sequence_number=3,
        content="What Pepsi products do you have?",
        operational_clarification=None,
    )
    session = MemorySession(owner)
    session.conversation = SimpleNamespace(business_id=business_id, last_message_at=NOW)
    user = SimpleNamespace(id=user_id)
    source = SimpleNamespace(
        id=source_id,
        updated_at=NOW,
        mapping_profile_key="discovered_products",
    )
    products = (product(), product("0008", "PEPSI DIET MINI", "0008"))
    registry = build_operational_tool_registry(timeout_seconds=2)
    definitions = (
        ProviderToolDefinition(**registry["product_search"].provider_schema()),
    )
    reads = []

    def execute(**kwargs):
        assert kwargs["business_id"] == business_id and kwargs["user"] is user
        assert kwargs["tool_name"] == "product_search"
        reads.append(kwargs["arguments"])
        key = kwargs["arguments"].get("external_product_id")
        query = kwargs["arguments"].get("query", "").casefold()
        items = tuple(
            item
            for item in products
            if (
                item.external_product_id == key
                if key is not None
                else any(query in name.casefold() for name in item.names)
            )
        )
        return OperationalToolResult(
            tool_name="product_search",
            output=catalogue(*items),
            source_id=source_id,
            latency_ms=0,
            source_updated_at=NOW,
            schema_fingerprint="a" * 64,
            source_fingerprint="b" * 64,
        )

    executor = SimpleNamespace(
        registry=registry,
        _profiles=object(),
        execute=execute,
        _active_source=lambda _business_id: source,
        sales_reporting_context=lambda *_args: ("Asia/Beirut", ()),
    )
    state = SimpleNamespace(pending=None, selected=None)

    def prepare(*_args, **_kwargs):
        pending = state.pending
        request = OwnerChatRequest(
            profile=PROFILE,
            knowledge=(),
            requested_at=NOW,
            messages=(ProviderMessage("owner", owner.content),),
            mode="operational",
            tools=definitions,
            pending_clarification=(
                {"operation": "product_search", "expected_reply": "selection"}
                if pending
                else None
            ),
        )
        return owner_chat._PreparedTurn(
            request=request,
            business=SimpleNamespace(default_language="en"),
            has_usable_evidence=True,
        )

    monkeypatch.setattr(owner_chat, "_build_operational_request", prepare)
    monkeypatch.setattr(
        owner_chat, "_pending_sales_clarification", lambda *_args: False
    )
    monkeypatch.setattr(
        owner_chat, "_pending_product_clarification", lambda *_args: state.pending
    )
    monkeypatch.setattr(
        owner_chat, "_selected_catalogue_context", lambda *_args: state.selected
    )
    monkeypatch.setattr(
        owner_chat, "_complete_pending_preference_if_selected", lambda *_args: None
    )
    monkeypatch.setattr(
        owner_chat, "_supersede_pending_preference", lambda *_args: None
    )

    return SimpleNamespace(
        session=session,
        owner=owner,
        business_id=business_id,
        user=user,
        source=source,
        executor=executor,
        definitions=definitions,
        state=state,
        reads=reads,
        claim=owner_chat._Claim(owner.id, uuid.uuid4()),
    )


def offline_provider(*plans):
    calls = []
    remaining = list(plans)

    def respond(request):
        calls.append(json.loads(request.content))
        assert remaining, "Unexpected additional model call"
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": json.dumps(remaining.pop(0))}]},
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 80,
                    "totalTokenCount": 180,
                },
            },
        )

    return GeminiOwnerChatProvider(
        api_key="offline-only",
        model="offline-fixture",
        timeout_seconds=2,
        transport=httpx.MockTransport(respond),
    ), calls


def plan(query="Pepsi", **extra):
    return dict(
        decision="tool",
        semantic_operation="product_search",
        tool_name="product_search",
        arguments={"query": query},
        **extra,
    )


def run(flow, provider, budget=None):
    return owner_chat._run_operational_loop(
        flow.session,
        flow.business_id,
        flow.claim,
        flow.user,
        provider,
        Settings(_env_file=None),
        flow.executor,
        flow.definitions,
        budget=budget,
    )


@pytest.mark.parametrize(
    ("message", "show_id", "show_sku"),
    [
        ("What's the ID of PEPSI MINI?", True, False),
        ("What's the SKU of PEPSI MINI?", False, True),
        ("Show PEPSI MINI with ID and SKU", True, True),
        ("شو معرف PEPSI MINI", True, False),
        ("shu sku PEPSI MINI", False, True),
    ],
)
def test_direct_identifier_requests_keep_the_real_single_result(
    flow, message, show_id, show_sku
):
    flow.owner.content = message
    provider, calls = offline_provider(plan("PEPSI MINI"))
    result, usage = run(flow, provider)
    assert ("ID: 0007" in result.reply) is show_id
    assert ("SKU: 0007" in result.reply) is show_sku
    assert "PEPSI DIET MINI" not in result.reply
    assert (
        flow.owner.operational_clarification["context"]["external_product_id"] == "0007"
    )
    assert usage.total_tokens == 180 and len(calls) == len(flow.reads) == 1


def test_list_then_selection_then_contextual_sku_rereads_exact_source_id(flow):
    provider, calls = offline_provider(plan())
    reply, usage = run(flow, provider)
    assert "1. PEPSI MINI\n2. PEPSI DIET MINI" in reply.reply
    pending = PendingCatalogueClarification.model_validate(
        flow.owner.operational_clarification
    )
    assert pending.mapping_version == 1 and pending.expires_at == NOW + timedelta(
        minutes=15
    )
    assert usage.total_tokens == 180 and len(calls) == 1
    flow.state.pending = pending
    flow.owner.content = "PEPSI MINI"
    provider, _ = offline_provider(plan("PEPSI MINI", pending_reply="selection"))
    result, _ = run(flow, provider)
    assert "ID:" not in result.reply and "SKU:" not in result.reply
    assert flow.reads[-1] == {
        "external_product_id": "0007",
        "mapping_version": 1,
        "limit": 20,
    }
    selected = owner_chat._SelectedCatalogueProduct.model_validate(
        flow.owner.operational_clarification["context"]
    )
    flow.state.pending = None
    flow.state.selected = selected
    flow.owner.content = "What's its SKU?"
    provider, calls = offline_provider(plan("its"))
    result, usage = run(flow, provider)
    assert "SKU: 0007" in result.reply and "ID:" not in result.reply
    assert flow.reads[-1] == {
        "external_product_id": "0007",
        "mapping_version": 1,
        "limit": 20,
    }
    assert (
        flow.owner.operational_clarification["context"]["expires_at"]
        == (selected.model_dump(mode="json")["expires_at"])
    )
    assert len(calls) == 1 and usage.total_tokens == 180


@pytest.mark.parametrize("message", ["Show their IDs", "Show their IDs and SKUs"])
def test_plural_identifier_followup_preserves_candidates_and_expiry(flow, message):
    provider, _ = offline_provider(plan())
    run(flow, provider)
    previous = flow.owner.operational_clarification
    flow.state.pending = PendingCatalogueClarification.model_validate(previous)
    flow.owner.content = message
    provider, calls = offline_provider(plan("their", pending_reply="unresolved"))
    result, usage = run(flow, provider)
    assert "ID: 0007" in result.reply and "ID: 0008" in result.reply
    assert ("SKU:" in result.reply) is ("SKUs" in message)
    assert "Which catalogue product" not in result.reply
    assert flow.owner.operational_clarification == previous
    assert len(flow.reads) == 1 and len(calls) == 1 and usage.total_tokens == 180


@pytest.mark.parametrize("message", ["eh", "yes", "okay", "تمام", "اي", "ah"])
def test_pending_acknowledgement_never_selects_a_product(flow, message):
    provider, _ = offline_provider(plan())
    first, _ = run(flow, provider)
    previous = flow.owner.operational_clarification
    flow.state.pending = PendingCatalogueClarification.model_validate(previous)
    flow.owner.content = message
    provider, _ = offline_provider(plan("PEPSI MINI", pending_reply="unresolved"))
    reply, _ = run(flow, provider)
    assert reply.reply == first.reply
    assert flow.owner.operational_clarification == previous and len(flow.reads) == 1


def test_singular_identifier_followup_with_multiple_candidates_does_not_select(flow):
    provider, _ = offline_provider(plan())
    run(flow, provider)
    flow.state.pending = PendingCatalogueClarification.model_validate(
        flow.owner.operational_clarification
    )
    flow.owner.content = "What's its SKU?"
    provider, _ = offline_provider(plan("its", pending_reply="unresolved"))
    result, _ = run(flow, provider)
    assert result.reply.startswith("Which catalogue product do you mean?")
    assert "SKU: 0007" in result.reply and "SKU: 0008" in result.reply
    assert len(flow.reads) == 1


def test_identifier_followup_without_a_current_selection_never_executes(flow):
    flow.owner.content = "What's its SKU?"
    provider, _ = offline_provider(plan("Pepsi"))
    result, usage = run(flow, provider)
    assert "no unambiguous, current product reference" in result.reply
    assert flow.reads == [] and usage.total_tokens == 180


@pytest.mark.parametrize("message", ["eh", "yes", "okay", "تمام", "اي", "ah"])
def test_stock_unavailable_then_acknowledgement_uses_provider_conversation(
    flow, monkeypatch, message
):
    provider, calls = offline_provider(
        {
            "decision": "unavailable",
            "semantic_operation": "inventory_product",
            "entity_kind": "product",
            "entity_query": "Pepsi",
            "reply": "999 in stock",
        },
        {"decision": "final", "semantic_operation": "conversation"},
        {
            "reply": "Understood. I can help with catalogue lookup.",
            "requires_business_knowledge": False,
        },
    )
    flow.owner.content = "How much Pepsi stock is there?"
    unavailable, stock_usage = run(flow, provider)
    assert "unknown" in unavailable.reply and "999" not in unavailable.reply
    assert flow.owner.operational_clarification is None
    flow.owner.content = message

    def prepare_conversation(*_args):
        return owner_chat._PreparedTurn(
            business=SimpleNamespace(default_language="en"),
            has_usable_evidence=True,
            request=OwnerChatRequest(
                profile=PROFILE,
                knowledge=(),
                requested_at=NOW,
                mode="conversation",
                messages=(
                    ProviderMessage("owner", "How much Pepsi stock is there?"),
                    ProviderMessage("assistant", unavailable.reply),
                    ProviderMessage("owner", message),
                ),
            ),
        )

    monkeypatch.setattr(owner_chat, "_build_conversation_request", prepare_conversation)
    reply, usage = run(flow, provider)
    assert reply.reply == "Understood. I can help with catalogue lookup."
    assert flow.reads == [] and len(calls) == 3
    assert stock_usage.total_tokens == 180 and usage.total_tokens == 360
    instructions = calls[-1]["systemInstruction"]["parts"][0]["text"]
    assert "Do not infer frustration, anger, confusion" in instructions
    assert "without explicit evidence" in instructions
    assert "revive an earlier product or inventory filter" in instructions
    assert calls[-1]["contents"][-2]["parts"][0]["text"] == unavailable.reply
    assert "business_profile" not in instructions


@pytest.mark.parametrize(
    "question",
    [
        "how much we have of PEPSI 330ML",
        "how much do we have",
        "how many Pepsi do we have",
    ],
)
def test_explicit_quantity_cannot_fall_back_to_catalogue_when_stock_is_unapproved(
    flow, question
):
    flow.owner.content = question
    provider, calls = offline_provider(plan("PEPSI"))
    result, usage = run(flow, provider)
    assert "Inventory access has not been approved" in result.reply
    assert flow.reads == [] and len(calls) == 1
    assert usage.total_tokens == 180


def selected_context(flow):
    return owner_chat._SelectedCatalogueProduct(
        business_id=flow.business_id,
        user_id=flow.user.id,
        conversation_id=flow.owner.conversation_id,
        source_id=flow.source.id,
        source_updated_at=NOW,
        mapping_version=1,
        schema_fingerprint="a" * 64,
        source_fingerprint="b" * 64,
        expires_at=NOW + timedelta(minutes=15),
        external_product_id="0007",
    )


def test_unsupported_new_product_request_clears_a_previous_selection(flow):
    flow.state.selected = selected_context(flow)
    flow.owner.content = "How much Coca-Cola stock is there?"
    provider, _ = offline_provider(
        {
            "decision": "unavailable",
            "semantic_operation": "inventory_product",
            "entity_kind": "product",
            "entity_query": "Coca-Cola",
            "reply": "999 in stock",
        }
    )
    reply, _ = run(flow, provider)
    assert "unknown" in reply.reply and flow.reads == []
    assert flow.owner.operational_clarification == {
        "operation": "catalogue_context_cleared"
    }


def test_not_found_search_clears_the_previous_product_context(flow):
    flow.state.selected = selected_context(flow)
    flow.owner.content = "Find an unrelated absent product"
    provider, _ = offline_provider(plan("absent product"))
    reply, _ = run(flow, provider)
    assert "No matching catalogue product" in reply.reply
    assert flow.owner.operational_clarification == {
        "operation": "catalogue_context_cleared"
    }


def test_unicode_candidates_and_long_request_fit_existing_state_bound(flow):
    output = catalogue(
        *(
            product(str(index), "ب" * 255, "sku-" + str(index)).model_copy(
                update={"categories": ("ع" * 255, "ج" * 255)}
            )
            for index in range(50)
        )
    )
    original = output.model_dump(mode="json")
    flow.owner.content = "List products " + "ع" * 4_000 + " with IDs"
    original_request = flow.owner.content
    executed = OperationalToolResult(
        tool_name="product_search",
        output=output,
        latency_ms=0,
        source_id=flow.source.id,
        source_updated_at=NOW,
        schema_fingerprint="a" * 64,
        source_fingerprint="b" * 64,
    )
    owner_chat._store_catalogue_clarification(
        flow.session, flow.user, flow.business_id, flow.owner.id, executed
    )
    state = flow.owner.operational_clarification
    assert len(json.dumps(state, ensure_ascii=False).encode("utf-8")) <= 8_192
    assert state["candidates"] == original["items"][: len(state["candidates"])]
    assert state["truncated"] and state["owner_request"] == "List products IDs"
    assert (
        flow.owner.content == original_request
        and output.model_dump(mode="json") == original
    )
    reply = owner_chat._catalogue_reply(output, original_request)
    assert len(reply) <= 14_000 and (
        "Results are truncated" in reply or "النتائج محدودة" in reply
    )


def test_selected_context_stores_a_scoped_key_instead_of_large_catalogue_metadata(flow):
    flow.owner.content = "Product details"
    item = product().model_copy(
        update={
            "names": ("ب" * 255, "ع" * 255, "ت" * 255),
            "categories": ("ف" * 255, "ج" * 255, "ك" * 255),
        }
    )
    executed = OperationalToolResult(
        tool_name="product_search",
        output=catalogue(item),
        latency_ms=0,
        source_id=flow.source.id,
        source_updated_at=NOW,
        schema_fingerprint="a" * 64,
        source_fingerprint="b" * 64,
    )
    owner_chat._store_catalogue_clarification(
        flow.session, flow.user, flow.business_id, flow.owner.id, executed
    )
    state = flow.owner.operational_clarification
    assert state["context"]["external_product_id"] == "0007"
    assert "product" not in state["context"]
    assert len(json.dumps(state, ensure_ascii=False).encode("utf-8")) <= 8_192
    assert executed.output.items == (item,)


@pytest.mark.parametrize(
    "field",
    [
        "business_id",
        "user_id",
        "conversation_id",
        "source_id",
        "source_updated_at",
        "mapping_version",
        "schema_fingerprint",
        "source_fingerprint",
        "expires_at",
    ],
)
def test_pending_choices_reject_stale_or_foreign_scope_without_product_reads(
    flow, monkeypatch, field
):
    context = selected_context(flow).model_dump()
    context.pop("external_product_id")
    pending = PendingCatalogueClarification(
        **context,
        candidates=(product(), product("0008", "PEPSI DIET MINI")),
        truncated=False,
        owner_request="List Pepsi products",
    )
    state = pending.model_dump(mode="json")
    state[field] = (
        str(uuid.uuid4())
        if field.endswith("_id")
        else "c" * 64
        if field.endswith("fingerprint")
        else 2
        if field == "mapping_version"
        else (NOW - timedelta(seconds=1)).isoformat()
    )
    monkeypatch.setattr(
        owner_chat, "_previous_product_clarification", lambda *_args: state
    )
    adapter = owner_chat.CatalogueSourceScope(1, "a" * 64, "b" * 64)
    flow.executor.catalogue_scope = lambda *_args: adapter
    assert (
        ORIGINAL_PENDING_LOADER(
            flow.session, flow.executor, flow.user, flow.business_id, flow.owner.id
        )
        is None
    )
    assert flow.reads == []


@pytest.mark.parametrize(
    "field",
    [
        "business_id",
        "user_id",
        "conversation_id",
        "source_id",
        "source_updated_at",
        "mapping_version",
        "schema_fingerprint",
        "source_fingerprint",
        "expires_at",
    ],
)
def test_selected_context_rejects_foreign_expired_or_stale_scope(flow, field):
    context = selected_context(flow)
    value = (
        uuid.uuid4()
        if field.endswith("_id")
        else "c" * 64
        if field.endswith("fingerprint")
        else 2
        if field == "mapping_version"
        else NOW - timedelta(seconds=1)
    )
    invalid = context.model_copy(update={field: value})
    adapter = owner_chat.CatalogueSourceScope(1, "a" * 64, "b" * 64)
    assert owner_chat._catalogue_context_scope_matches(
        context, flow.owner, flow.user, flow.business_id, flow.source, adapter
    )
    assert not owner_chat._catalogue_context_scope_matches(
        invalid, flow.owner, flow.user, flow.business_id, flow.source, adapter
    )


def test_context_loader_is_bounded_scoped_and_new_search_invalidates_selection(
    flow, monkeypatch
):
    context = selected_context(flow)
    flow.session.scalar_result = SimpleNamespace(
        content=flow.owner.content,
        operational_clarification={
            "operation": "catalogue_product",
            "context": context.model_dump(mode="json"),
        },
    )
    adapter = owner_chat.CatalogueSourceScope(1, "a" * 64, "b" * 64)
    flow.executor.catalogue_scope = lambda *_args: adapter
    # Exercise the production loader, which the loop fixture otherwise replaces.
    assert (
        ORIGINAL_CONTEXT_LOADER(
            flow.session, flow.executor, flow.user, flow.business_id, flow.owner.id
        )
        == context
    )
    compiled = flow.session.statements[-1].compile(dialect=postgresql.dialect())
    assert flow.owner.conversation_id in compiled.params.values()
    assert "sequence_number >=" in str(compiled) and "LIMIT" in str(compiled)
    flow.session.scalar_result.operational_clarification = {
        "operation": "product_search"
    }
    assert (
        ORIGINAL_CONTEXT_LOADER(
            flow.session, flow.executor, flow.user, flow.business_id, flow.owner.id
        )
        is None
    )


ORIGINAL_CONTEXT_LOADER = owner_chat._selected_catalogue_context
ORIGINAL_PENDING_LOADER = owner_chat._pending_product_clarification


def test_budget_reserves_and_resizes_actual_stages_without_database_access(
    flow, monkeypatch
):
    events = []
    reservation = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(owner_chat, "_admit_provider_generation", lambda *_args: 1)
    monkeypatch.setattr(
        owner_chat,
        "reserve_owner_chat_usage",
        lambda *_args, **kw: events.append(("reserve", kw)) or reservation,
    )
    monkeypatch.setattr(
        owner_chat,
        "resize_owner_chat_usage",
        lambda *_args, **kw: events.append(("resize", kw)),
    )
    budget = owner_chat._OperationalUsageBudget(
        flow.session,
        SimpleNamespace(id=flow.business_id),
        flow.user,
        flow.claim,
        Settings(_env_file=None),
    )
    provider = SimpleNamespace(estimate_input_tokens=lambda _request: 100)
    request = OwnerChatRequest(
        profile=PROFILE, knowledge=(), messages=(), requested_at=NOW
    )
    budget.admit(provider, request)
    budget.usage = TokenUsage(100, 80, 180, authoritative=True)
    budget.admit(provider, request)
    assert [event[0] for event in events] == ["reserve", "resize"]
    assert events[0][1]["max_output_tokens"] == 512
    assert events[1][1]["estimated_input_tokens"] == 200
    assert events[1][1]["max_output_tokens"] == 592
    assert budget.calls == 2 and budget.reservation is reservation
    budget.admit(provider, request)
    with pytest.raises(OwnerChatProviderInvalidResponse):
        budget.admit(provider, request)
    assert len(events) == 3


def test_persistence_reconciles_the_same_usage_with_the_assistant(flow, monkeypatch):
    flow.owner.generation_state = ChatGenerationState.PROCESSING
    flow.owner.generation_claim_token = flow.claim.token
    flow.session.scalar_result = flow.owner
    flow.owner.generation_claim_expires_at = NOW + timedelta(minutes=3)
    original_scalar = flow.session.scalar
    monkeypatch.setattr(
        flow.session,
        "scalar",
        lambda statement: (
            NOW
            if "clock_timestamp" in str(statement) and "FROM" not in str(statement)
            else original_scalar(statement)
        ),
    )
    monkeypatch.setattr(owner_chat, "upsert_proposed_knowledge", lambda *_args: None)
    reconciled = []
    monkeypatch.setattr(
        owner_chat, "reconcile_ai_usage", lambda *_args, **kw: reconciled.append(kw)
    )
    usage = TokenUsage(100, 80, 180, authoritative=True)
    result = OwnerChatResult(
        reply="Catalogue product: PEPSI MINI.",
        usage=usage,
        provider_identifier="gemini",
        model_identifier="offline-fixture",
    )
    owner_chat._persist_result(
        flow.session,
        flow.business_id,
        flow.claim,
        result,
        SimpleNamespace(id=uuid.uuid4()),
        usage,
        (),
    )
    assert flow.session.added[0].content == result.reply
    assert flow.owner.generation_state == ChatGenerationState.COMPLETED
    assert reconciled[0]["usage"] is usage and reconciled[0]["commit"] is False
    assert reconciled[0]["require_new"] is True
    assert flow.session.commits == 1


@pytest.mark.parametrize("failed", [False, True])
def test_terminal_replay_never_calls_provider_source_or_accounting(
    flow, monkeypatch, failed
):
    flow.owner.generation_state = (
        ChatGenerationState.FAILED if failed else ChatGenerationState.COMPLETED
    )
    flow.owner.role = ChatMessageRole.OWNER
    flow.owner.created_at = NOW
    flow.owner.reply_to_message_id = None
    flow.owner.citations = []
    flow.session.scalar_result = SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=flow.owner.conversation_id,
        sequence_number=4,
        role=ChatMessageRole.ASSISTANT,
        created_at=NOW,
        content="1. PEPSI MINI\n2. PEPSI DIET MINI",
        reply_to_message_id=flow.owner.id,
        generation_state=None,
        citations=[],
    )
    monkeypatch.setattr(owner_chat, "_eligible_business", lambda *_args: None)
    monkeypatch.setattr(
        owner_chat,
        "get_default_conversation",
        lambda *_args, **kw: SimpleNamespace(id=flow.owner.conversation_id),
    )
    monkeypatch.setattr(
        owner_chat,
        "_create_or_reuse_owner_message",
        lambda *_args: (flow.owner, True, None),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Replay reached execution or accounting")

    monkeypatch.setattr(owner_chat, "_generate_claimed_turn", forbidden)
    monkeypatch.setattr(owner_chat, "reconcile_ai_usage", forbidden)
    provider = SimpleNamespace(generate=forbidden, estimate_input_tokens=forbidden)
    if failed:
        flow.session.scalar_result = None
        with pytest.raises(ApplicationError) as caught:
            owner_chat.submit_owner_message(
                flow.session,
                flow.user,
                flow.business_id,
                OwnerMessageRequest(
                    content=flow.owner.content, idempotency_key="offline-replay"
                ),
                provider,
                Settings(_env_file=None),
            )
        assert caught.value.error_code == "owner_turn_failed"
        assert flow.session.commits == 0 and flow.reads == []
        return
    result = owner_chat.submit_owner_message(
        flow.session,
        flow.user,
        flow.business_id,
        OwnerMessageRequest(
            content=flow.owner.content, idempotency_key="offline-replay"
        ),
        provider,
        Settings(_env_file=None),
    )
    assert result.replayed and result.assistant_message.content.startswith(
        "1. PEPSI MINI"
    )
    assert flow.session.commits == 0 and flow.reads == []


def pending_list(flow):
    provider, _ = offline_provider(plan())
    run(flow, provider)
    flow.state.pending = PendingCatalogueClarification.model_validate(
        flow.owner.operational_clarification
    )
    flow.reads.clear()
    return flow.state.pending


def usage_budget(flow, monkeypatch):
    events = []
    monkeypatch.setattr(owner_chat, "_admit_provider_generation", lambda *_args: 1)
    monkeypatch.setattr(owner_chat.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(
        owner_chat,
        "reserve_owner_chat_usage",
        lambda *_args, **kw: (
            events.append(("reserve", kw))
            or SimpleNamespace(id=uuid.uuid4(), reset_at=NOW + timedelta(days=1))
        ),
    )
    monkeypatch.setattr(
        owner_chat,
        "resize_owner_chat_usage",
        lambda *_args, **kw: events.append(("resize", kw)),
    )
    budget = owner_chat._OperationalUsageBudget(
        flow.session,
        SimpleNamespace(id=flow.business_id),
        flow.user,
        flow.claim,
        Settings(_env_file=None),
    )
    return budget, events


@pytest.mark.parametrize("selection", ["PEPSI MINI", "0007", "pepsi mini!"])
def test_exact_selection_needs_no_provider_and_rereads_source(flow, selection):
    pending_list(flow)
    flow.owner.content = selection
    provider, calls = offline_provider()
    result, usage = run(flow, provider)
    assert calls == [] and usage.total_tokens == 0
    assert len(flow.reads) == 1 and flow.reads[0]["external_product_id"] == "0007"
    assert (
        "PEPSI MINI" in result.reply and "Stock and prices are unknown" in result.reply
    )
    assert "ID:" not in result.reply and "SKU:" not in result.reply
    assert flow.owner.operational_clarification["operation"] == "catalogue_product"


def test_duplicate_exact_names_require_planning_and_cannot_select(flow):
    pending = pending_list(flow)
    flow.state.pending = pending.model_copy(
        update={
            "candidates": (product("0007"), product("0009")),
        }
    )
    flow.owner.content = "PEPSI MINI"
    provider, calls = offline_provider(plan("PEPSI MINI", pending_reply="selection"))
    result, _ = run(flow, provider)
    assert len(calls) == 1 and flow.reads == []
    assert "0007" in result.reply and "0009" in result.reply


@pytest.mark.parametrize(
    "field,value",
    [
        ("expires_at", NOW),
        ("business_id", uuid.uuid4()),
        ("user_id", uuid.uuid4()),
        ("conversation_id", uuid.uuid4()),
    ],
)
def test_fast_selection_rejects_expired_or_cross_scope(flow, field, value):
    pending = pending_list(flow).model_copy(update={field: value})
    flow.owner.content = "PEPSI MINI"
    assert (
        owner_chat._read_exact_catalogue_selection(
            flow.session,
            flow.executor,
            flow.user,
            flow.business_id,
            flow.owner,
            pending,
        )
        is None
    )
    assert flow.reads == []


@pytest.mark.parametrize(
    "mismatch", ["id", "mapping", "source", "stamp", "schema", "fingerprint", "missing"]
)
def test_fast_selection_verifies_fresh_exact_result(flow, mismatch):
    pending = pending_list(flow)
    flow.owner.content = "PEPSI MINI"
    execute = flow.executor.execute

    def changed(**kwargs):
        fresh = execute(**kwargs)
        changes = {
            "id": {"output": catalogue(product("wrong"))},
            "mapping": {
                "output": fresh.output.model_copy(update={"mapping_version": 2})
            },
            "source": {"source_id": uuid.uuid4()},
            "stamp": {"source_updated_at": NOW + timedelta(seconds=1)},
            "schema": {"schema_fingerprint": "c" * 64},
            "fingerprint": {"source_fingerprint": "d" * 64},
            "missing": {
                "output": CatalogueResult(
                    mapping_version=1, status="not_found", items=(), truncated=False
                )
            },
        }
        return replace(fresh, **changes[mismatch])

    flow.executor.execute = changed
    result = owner_chat._read_exact_catalogue_selection(
        flow.session, flow.executor, flow.user, flow.business_id, flow.owner, pending
    )
    assert "new search" in result.reply
    assert (
        flow.owner.operational_clarification["operation"] == "catalogue_context_cleared"
    )


@pytest.mark.parametrize(
    "message", ["hi", "hello!", "hey", "مرحبا", "أهلا", "mar7aba", "salam"]
)
@pytest.mark.parametrize("retry", [False, True])
def test_clear_greeting_routes_to_generated_conversation_and_keeps_pending(
    flow, monkeypatch, message, retry
):
    pending = pending_list(flow)
    flow.owner.content = message
    business = SimpleNamespace(id=flow.business_id, default_language="en")
    original_get = flow.session.get
    monkeypatch.setattr(
        flow.session,
        "get",
        lambda model, key: business if model is Business else original_get(model, key),
    )
    monkeypatch.setattr(
        owner_chat, "OperationalToolExecutor", lambda *_args: flow.executor
    )
    monkeypatch.setattr(
        owner_chat,
        "_build_conversation_request",
        lambda *_args: owner_chat._PreparedTurn(
            business=business,
            has_usable_evidence=True,
            request=OwnerChatRequest(
                profile=PROFILE,
                knowledge=(),
                messages=(ProviderMessage("owner", message),),
                requested_at=NOW,
                mode="conversation",
            ),
        ),
    )
    _, events = usage_budget(flow, monkeypatch)
    saved = []
    monkeypatch.setattr(
        owner_chat, "_persist_result", lambda *_args: saved.append(_args)
    )
    provider, calls = offline_provider(
        {"reply": "Provider greeting", "requires_business_knowledge": False}
    )
    successful_transport = provider.transport
    attempts = []

    def respond(request):
        attempts.append(request)
        if retry and len(attempts) == 1:
            return httpx.Response(503)
        return successful_transport.handle_request(request)

    provider.transport = httpx.MockTransport(respond)
    assert not owner_chat._generate_claimed_turn(
        flow.session,
        flow.business_id,
        flow.claim,
        flow.user,
        provider,
        Settings(_env_file=None),
        object(),
    )
    assert len(calls) == 1 and "approved_tools" not in str(calls[0])
    expected_context_flag = (
        "catalogue_choices_available"
        if pending.state_kind == "discovery"
        else "catalogue_choice_pending"
    )
    assert expected_context_flag in str(calls[0])
    assert flow.owner.operational_clarification == pending.model_dump(mode="json")
    assert saved[0][3].reply == "Provider greeting"
    assert saved[0][5].total_tokens == 180 + (
        events[0][1]["estimated_input_tokens"] + 512 if retry else 0
    )
    assert len(events) == len(attempts) == (2 if retry else 1) and flow.reads == []


@pytest.mark.parametrize(
    "message",
    [
        "hi show Pepsi",
        "hello stock",
        "مرحبا شو عندك بيبسي",
        "mar7aba shu 3andak",
        "eh",
        "yes",
        "okay",
        "تمام",
    ],
)
def test_greeting_routing_is_conservative(message):
    assert not owner_chat._is_clear_greeting(message)


@pytest.mark.parametrize("status", [500, 502, 503, 504, 429, 401])
def test_gemini_failure_diagnostics_are_sanitized(status, caplog):
    request = OwnerChatRequest(
        profile=PROFILE,
        knowledge=(),
        messages=(ProviderMessage("owner", "PRIVATE CATALOGUE"),),
        requested_at=NOW,
        mode="conversation",
    )
    provider = GeminiOwnerChatProvider(
        api_key="private-api-key",
        model="offline-fixture",
        timeout_seconds=2,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                status,
                headers={
                    "Retry-After": "2",
                    "x-request-id": "12345678-abcd-1234-abcd-123456789abc",
                },
                json={
                    "error": {
                        "status": "UNAVAILABLE",
                        "message": "private body private-api-key PRIVATE CATALOGUE",
                    }
                },
            )
        ),
    )
    with pytest.raises(OwnerChatProviderUnavailable) as caught:
        provider.generate(request)
    failure = caught.value
    assert failure.upstream_http_status == status
    assert (
        failure.google_error_status == "UNAVAILABLE"
        and failure.retry_after_seconds == 2
    )
    assert failure.provider_request_id == "12345678-abcd-1234-abcd-123456789abc"
    assert failure.request_mode == "conversation" and failure.elapsed_ms >= 0
    assert "private-api-key" not in caplog.text and "private body" not in caplog.text
    assert "PRIVATE CATALOGUE" not in caplog.text
    assert f"upstream_status={status}" in caplog.text


def test_diagnostics_reject_untrusted_status_headers_and_dates(caplog):
    provider = GeminiOwnerChatProvider(
        api_key="private-api-key", model="offline-fixture", timeout_seconds=2
    )
    for google_status in (["UNAVAILABLE"], "PRIVATE CATALOGUE", {"status": "INTERNAL"}):
        response = httpx.Response(
            503,
            headers={
                "x-request-id": "private-api-key-123456789",
                "retry-after": "private",
            },
            json={"error": {"status": google_status}},
        )
        failure = provider._http_error(503, None, response)
        assert (
            failure.google_error_status is None and failure.provider_request_id is None
        )
        assert failure.retry_after_seconds is None
    response = httpx.Response(
        503, headers={"retry-after": "Mon, 12 Oct 2037 00:00:00 GMT"}
    )
    assert provider._http_error(503, None, response).retry_after_seconds == 86400


@pytest.mark.parametrize(
    "timeout",
    [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout, httpx.PoolTimeout],
)
def test_timeout_preserves_subtype_and_safe_message(timeout, caplog):
    def failed(request):
        raise timeout("private-api-key and prompt", request=request)

    provider = GeminiOwnerChatProvider(
        api_key="private-api-key",
        model="offline-fixture",
        timeout_seconds=2,
        transport=httpx.MockTransport(failed),
    )
    request = OwnerChatRequest(
        profile=PROFILE,
        knowledge=(),
        messages=(ProviderMessage("owner", "hi"),),
        requested_at=NOW,
        mode="conversation",
    )
    with pytest.raises(OwnerChatProviderTimeout) as caught:
        provider.generate(request)
    assert (
        caught.value.timeout_subtype == timeout.__name__
        and caught.value.reason == "timeout"
    )
    assert caught.value.upstream_http_status is None
    assert "private-api-key" not in caplog.text


@pytest.mark.parametrize("status", [500, 502, 503, 504, 429])
def test_one_retry_accounts_every_attempt_and_preserves_full_unknown_charge(
    flow, monkeypatch, status
):
    budget, events = usage_budget(flow, monkeypatch)
    delays = []
    monkeypatch.setattr(owner_chat.time, "sleep", delays.append)
    provider, calls = offline_provider(
        {"reply": "Safe reply", "requires_business_knowledge": False}
    )
    success = provider.transport
    dispatches = []

    def respond(request):
        dispatches.append(request)
        if len(dispatches) == 1:
            return httpx.Response(
                status,
                headers={"retry-after": "1"},
                json={"error": {"status": "UNAVAILABLE"}},
            )
        return success.handle_request(request)

    provider.transport = httpx.MockTransport(respond)
    request = OwnerChatRequest(
        profile=PROFILE,
        knowledge=(),
        messages=(ProviderMessage("owner", "hi"),),
        requested_at=NOW,
        mode="conversation",
    )
    estimate = provider.estimate_input_tokens(request)
    result = owner_chat._validated_generation(provider, request, budget)
    assert result.reply == "Safe reply" and len(dispatches) == 2 and budget.calls == 2
    first_charge = 0 if status == 429 else estimate + 512
    assert budget.usage.total_tokens == first_charge + 180
    assert [event[0] for event in events] == ["reserve", "resize"]
    assert events[1][1]["estimated_input_tokens"] == estimate + (
        0 if status == 429 else estimate
    )
    assert events[1][1]["max_output_tokens"] == 512 + (0 if status == 429 else 512)
    assert budget.retries == 1 and len(calls) == 1
    assert delays == [1]


@pytest.mark.parametrize(
    "block",
    [
        "second_failure",
        "long_retry_after",
        "expired_lease",
        "call_limit",
        "timeout",
        "auth",
        "validation",
    ],
)
def test_retry_is_bounded_and_never_weakens_accounting(flow, monkeypatch, block):
    budget, events = usage_budget(flow, monkeypatch)
    if block == "expired_lease":
        budget.deadline = NOW + timedelta(seconds=2)
    if block == "call_limit":
        budget.calls = 2
    dispatches = []

    def respond(request):
        dispatches.append(request)
        if block == "timeout":
            raise httpx.ReadTimeout("private", request=request)
        return httpx.Response(
            401 if block == "auth" else 400 if block == "validation" else 503,
            headers={"retry-after": "10" if block == "long_retry_after" else "1"},
            json={"error": {"status": "UNAVAILABLE"}},
        )

    provider = GeminiOwnerChatProvider(
        api_key="offline-only",
        model="offline-fixture",
        timeout_seconds=2,
        transport=httpx.MockTransport(respond),
    )
    request = OwnerChatRequest(
        profile=PROFILE,
        knowledge=(),
        messages=(ProviderMessage("owner", "hi"),),
        requested_at=NOW,
        mode="conversation",
    )
    with pytest.raises(
        (
            OwnerChatProviderUnavailable,
            OwnerChatProviderTimeout,
            OwnerChatProviderInvalidResponse,
        )
    ):
        owner_chat._validated_generation(provider, request, budget)
    count = 3 if block == "second_failure" else 1
    assert len(dispatches) == len(events) == count
    assert budget.usage.total_tokens == count * (
        provider.estimate_input_tokens(request) + 512
    )


def test_late_provider_response_is_charged_but_cannot_complete_turn(flow, monkeypatch):
    budget, _ = usage_budget(flow, monkeypatch)
    provider, _ = offline_provider(
        {"reply": "Too late", "requires_business_knowledge": False}
    )
    original = provider.generate

    def late(request):
        result = original(request)
        budget.deadline = NOW
        return result

    monkeypatch.setattr(provider, "generate", late)
    request = OwnerChatRequest(
        profile=PROFILE,
        knowledge=(),
        messages=(ProviderMessage("owner", "hi"),),
        requested_at=NOW,
        mode="conversation",
    )
    with pytest.raises(OwnerChatProviderTimeout) as caught:
        owner_chat._validated_generation(provider, request, budget)
    assert caught.value.reason == "turn_deadline" and budget.usage.total_tokens == 180
    assert budget.calls == 1 and budget.retries == 0


def test_retry_reservation_rejection_keeps_prior_uncertain_consumption(
    flow, monkeypatch
):
    budget, _ = usage_budget(flow, monkeypatch)

    def rejected(*_args, **_kwargs):
        raise ApplicationError("budget reached", status_code=429)

    monkeypatch.setattr(owner_chat, "resize_owner_chat_usage", rejected)
    provider = GeminiOwnerChatProvider(
        api_key="offline-only",
        model="offline-fixture",
        timeout_seconds=2,
        transport=httpx.MockTransport(lambda _request: httpx.Response(503)),
    )
    request = OwnerChatRequest(
        profile=PROFILE,
        knowledge=(),
        messages=(ProviderMessage("owner", "hi"),),
        requested_at=NOW,
        mode="conversation",
    )
    with pytest.raises(ApplicationError):
        owner_chat._validated_generation(provider, request, budget)
    assert (
        budget.calls == 1
        and budget.usage.total_tokens == provider.estimate_input_tokens(request) + 512
    )


def test_retry_charge_survives_operational_plan_and_catalogue_result(flow, monkeypatch):
    budget, _ = usage_budget(flow, monkeypatch)
    provider, _ = offline_provider(plan())
    success = provider.transport
    attempts = []

    def respond(request):
        attempts.append(request)
        return (
            httpx.Response(503)
            if len(attempts) == 1
            else success.handle_request(request)
        )

    provider.transport = httpx.MockTransport(respond)
    result, usage = run(flow, provider, budget)
    assert len(attempts) == 2 and len(flow.reads) == 1
    assert usage == budget.usage and usage.total_tokens > 180
    assert result.usage == budget.usage


def test_retry_and_success_share_the_three_call_limit_across_the_entire_turn(
    flow, monkeypatch
):
    budget, _ = usage_budget(flow, monkeypatch)
    provider, _ = offline_provider(
        {"reply": "Safe", "requires_business_knowledge": False}
    )
    successful_transport = provider.transport
    attempts = []

    def respond(request):
        attempts.append(request)
        return (
            successful_transport.handle_request(request)
            if len(attempts) == 2
            else httpx.Response(503)
        )

    provider.transport = httpx.MockTransport(respond)
    request = OwnerChatRequest(
        profile=PROFILE,
        knowledge=(),
        messages=(ProviderMessage("owner", "hi"),),
        requested_at=NOW,
        mode="conversation",
    )
    owner_chat._validated_generation(provider, request, budget)
    with pytest.raises(OwnerChatProviderUnavailable):
        owner_chat._validated_generation(provider, request, budget)
    assert len(attempts) == budget.calls == 3 and budget.retries == 1
    assert (
        budget.usage.total_tokens
        == 2 * (provider.estimate_input_tokens(request) + 512) + 180
    )


def test_two_transient_failures_back_off_exponentially_with_jitter(flow, monkeypatch):
    budget, events = usage_budget(flow, monkeypatch)
    delays = []
    monkeypatch.setattr(owner_chat.time, "sleep", delays.append)
    monkeypatch.setattr(owner_chat.random, "uniform", lambda low, high: 0.75)
    provider, _ = offline_provider(
        {"reply": "Recovered", "requires_business_knowledge": False}
    )
    successful_transport = provider.transport
    attempts = []

    def respond(request):
        attempts.append(request)
        if len(attempts) <= 2:
            return httpx.Response(503, json={"error": {"status": "UNAVAILABLE"}})
        return successful_transport.handle_request(request)

    provider.transport = httpx.MockTransport(respond)
    request = OwnerChatRequest(
        profile=PROFILE,
        knowledge=(),
        messages=(ProviderMessage("owner", "hi"),),
        requested_at=NOW,
        mode="conversation",
    )
    result = owner_chat._validated_generation(provider, request, budget)
    assert result.reply == "Recovered"
    assert delays == [0.75, 1.5]
    assert len(attempts) == len(events) == budget.calls == 3
    assert budget.retries == 2
    assert (
        budget.usage.total_tokens
        == 2 * (provider.estimate_input_tokens(request) + 512) + 180
    )


@pytest.mark.parametrize("seconds,attempt_count", [(30, 2), (10, 1)])
def test_late_stage_retry_uses_remaining_lease_without_requiring_full_timeout(
    flow, monkeypatch, seconds, attempt_count
):
    budget, events = usage_budget(flow, monkeypatch)
    budget.deadline = NOW + timedelta(seconds=seconds)
    budget.calls = 1  # The planner has already used one of the three calls.
    monkeypatch.setattr(owner_chat.random, "uniform", lambda *_: 0.75)
    provider, _ = offline_provider(
        {"reply": "Recovered", "requires_business_knowledge": False}
    )
    provider.timeout_seconds = 120
    success = provider.transport
    attempts = []

    def respond(request):
        attempts.append(request)
        return (
            httpx.Response(503)
            if len(attempts) == 1
            else success.handle_request(request)
        )

    provider.transport = httpx.MockTransport(respond)
    request = OwnerChatRequest(
        profile=PROFILE,
        knowledge=(),
        messages=(ProviderMessage("owner", "hi"),),
        requested_at=NOW,
        mode="conversation",
    )
    if attempt_count == 2:
        assert (
            owner_chat._validated_generation(provider, request, budget).reply
            == "Recovered"
        )
        assert budget.retries == 1 and budget.calls == 3
        assert budget.usage.total_tokens > 180
    else:
        with pytest.raises(OwnerChatProviderUnavailable):
            owner_chat._validated_generation(provider, request, budget)
        assert budget.retries == 0 and budget.calls == 2
    assert len(attempts) == len(events) == attempt_count


def test_dispatch_timeout_is_capped_to_remaining_claim(flow, monkeypatch):
    budget, _ = usage_budget(flow, monkeypatch)
    budget.deadline = NOW + timedelta(seconds=7)
    captured = []
    provider, _ = offline_provider(
        {"reply": "Safe", "requires_business_knowledge": False}
    )
    original = provider.generate

    def inspect(request):
        captured.append(request.provider_timeout_seconds)
        return original(request)

    monkeypatch.setattr(provider, "generate", inspect)
    request = OwnerChatRequest(
        profile=PROFILE,
        knowledge=(),
        messages=(ProviderMessage("owner", "hi"),),
        requested_at=NOW,
        mode="conversation",
    )
    owner_chat._validated_generation(provider, request, budget)
    assert 0 < captured[0] <= 7
    budget.deadline = NOW
    with pytest.raises(OwnerChatProviderTimeout):
        owner_chat._validated_generation(provider, request, budget)
    assert len(captured) == 1


def test_pending_planner_context_omits_presentation_and_deduplicates_display_names(
    flow, monkeypatch
):
    monkeypatch.setattr(
        owner_chat, "_validated_pending_preference", lambda *_args: None
    )
    pending = pending_list(flow)
    original_item = pending.candidates[0].model_copy(
        update={"names": ("PEPSI MINI", "PEPSI MINI")}
    )
    flow.state.pending = pending.model_copy(
        update={"candidates": (original_item, *pending.candidates[1:])}
    )
    context = owner_chat._operational_pending_context(
        flow.session, flow.executor, flow.user, flow.business_id, flow.owner
    )
    assert context is None
    context = owner_chat._catalogue_followup_context(
        flow.session, flow.executor, flow.user, flow.business_id, flow.owner
    ).model_dump(mode="json")
    assert "question" not in context and context["candidates"][0]["names"] == [
        "PEPSI MINI",
    ]
    assert flow.state.pending.candidates[0].names == ("PEPSI MINI", "PEPSI MINI")
    assert len(context["candidates"]) == len(pending.candidates)
    request = OwnerChatRequest(
        profile=PROFILE,
        knowledge=(),
        messages=(ProviderMessage("owner", "eh"),),
        requested_at=NOW,
        mode="operational",
        tools=flow.definitions,
        catalogue_context=owner_chat.CatalogueFollowupContext.model_validate(context),
    )
    provider, _ = offline_provider()
    old = replace(
        request,
        pending_clarification={
            "operation": "product_search",
            "question": "Prior rendered catalogue list. " * 20,
        },
    )
    assert provider.estimate_input_tokens(request) < provider.estimate_input_tokens(old)


@pytest.mark.parametrize("next_action", ["dispatch", "completion"])
def test_deadline_keeps_prior_usage_for_failure_reconciliation(
    flow, monkeypatch, next_action
):
    budget, events = usage_budget(flow, monkeypatch)
    provider, calls = offline_provider(
        {"reply": "Safe", "requires_business_knowledge": False}
    )
    request = OwnerChatRequest(
        profile=PROFILE,
        knowledge=(),
        messages=(ProviderMessage("owner", "hi"),),
        requested_at=NOW,
        mode="conversation",
    )
    owner_chat._validated_generation(provider, request, budget)
    budget.deadline = NOW
    with pytest.raises(OwnerChatProviderTimeout) as caught:
        if next_action == "dispatch":
            owner_chat._validated_generation(provider, request, budget)
        else:
            budget.ensure_active()
    assert caught.value.reason == "turn_deadline"
    assert caught.value.usage == budget.usage
    assert budget.usage.total_tokens == 180 and len(calls) == len(events) == 1


@pytest.mark.parametrize(
    "failure,code",
    [
        (OwnerChatProviderTimeout(reason="timeout"), "assistant_timeout"),
        (
            OwnerChatProviderUnavailable(reason="server_error", retry_after_seconds=2),
            "assistant_unavailable",
        ),
        (OwnerChatProviderUnavailable(reason="rate_limited"), "assistant_rate_limited"),
        (
            OwnerChatProviderInvalidResponse(reason="invalid_json"),
            "assistant_invalid_response",
        ),
    ],
)
def test_user_errors_distinguish_failure_without_provider_details(failure, code):
    error = owner_chat._safe_provider_failure(failure)
    assert error.error_code == code and error.status_code == 503
    assert "catalogue" not in error.detail and error.details is None
    if failure.retry_after_seconds:
        assert error.headers == {"Retry-After": "2"}


@pytest.mark.parametrize(
    "question,term,good,bad",
    [
        ("drinkable water", "water", "RIM WATER 2L", "IBIZA HAND SOAP COCO WATER"),
        ("water brands", "water", "PERRIER WATER", "NIVEA WATERMELON SHINE"),
        ("water", "water", "RIM WATER 2L", "TUNA WATER"),
        (
            "just give me the drinkable water dont give me something else",
            "water",
            "RIM WATER",
            "HAND SOAP COCO WATER",
        ),
        (
            "what we have sugar products",
            "sugar",
            "HARVEST SUGAR 5KG",
            "TOAST SUGAR FREE",
        ),
        (
            "something to drink",
            "drink",
            "ORANGE JUICE DRINK",
            "AIR FRESHENER DRINK SCENT",
        ),
        ("sugar products", "sugar", "HARVEST SUGAR 5KG", "TOAST SUGAR FREE"),
        ("rice varieties", "rice", "BASMATI RICE", "RICE FACE CREAM"),
        ("sho 3endak shi eshrabo", "water", "RIM WATER", "FABRIC SOFTENER WATER"),
        ("sho fi 3endak anwe3 roz", "rice", "LONG GRAIN RICE", "RICE SOAP"),
        ("شو عندك شي اشربه", "water", "AQUAFINA WATER", "WATER SOAP"),
        ("just the drinkable ones", "water", "RIM WATER", "TUNA WATER"),
        ("sparkling", "sparkling", "RIM SPARKLING WATER", "AIR WICK SPARKLING CITRUS"),
    ],
)
def test_semantic_candidates_are_filtered_before_display(
    flow, question, term, good, bad
):
    reads = []
    rows = catalogue(product("good", good), product("bad", bad))

    def execute(**kwargs):
        reads.append(kwargs["arguments"])
        return OperationalToolResult(
            tool_name="product_search",
            output=rows,
            latency_ms=0,
            source_id=flow.source.id,
            source_updated_at=NOW,
            schema_fingerprint="a" * 64,
            source_fingerprint="b" * 64,
        )

    flow.executor.execute = execute
    provider, calls = offline_provider(
        {"status": "matched", "candidate_references": ["0"]}
    )
    request = OwnerChatRequest(
        profile=PROFILE,
        knowledge=(),
        messages=(ProviderMessage("owner", question),),
        requested_at=NOW,
    )
    result, usage = owner_chat._search_relevant_catalogue(
        flow.executor,
        flow.user,
        flow.business_id,
        {"query": term, "semantic_query": question, "search_queries": [term, term]},
        provider,
        request,
        None,
    )
    assert result.output.items == (rows.items[0],)
    assert result.source_result == result.output
    assert len(reads) == 1 and reads[0] == {"query": term, "limit": 50}
    assert result.output.search_semantics == "semantic"
    assert result.output.continuation is None
    assert usage.total_tokens == 180
    assert "not keyword overlap" in calls[0]["systemInstruction"]["parts"][0]["text"]
    assert "good" not in calls[0]["systemInstruction"]["parts"][0]["text"]


@pytest.mark.parametrize("browse", [False, None])
def test_query_with_disabled_browse_is_canonicalized_before_execution(flow, browse):
    arguments = owner_chat._validated_provider_arguments(
        flow.executor,
        "product_search",
        {"query": "water", "semantic_query": "drinking water", "browse": browse},
    )
    assert arguments is not None and "browse" not in arguments
    assert arguments["query"] == "water"
    assert arguments["semantic_query"] == "drinking water"
    # The executable API still accepts only a query OR an explicit true browse.
    from app.schemas.source_mapping import CatalogueRequest

    CatalogueRequest.model_validate({"query": arguments["query"]})
    with pytest.raises(ValueError):
        CatalogueRequest.model_validate({"query": "water", "browse": False})


def test_relevance_failure_does_not_return_unfiltered_products(flow):
    provider, _ = offline_provider(
        {"status": "matched", "candidate_references": ["invented"]}
    )
    request = OwnerChatRequest(
        profile=PROFILE,
        knowledge=(),
        messages=(ProviderMessage("owner", "Pepsi"),),
        requested_at=NOW,
    )
    from app.tools.operational import ToolExecutionError

    with pytest.raises(ToolExecutionError) as caught:
        owner_chat._search_relevant_catalogue(
            flow.executor,
            flow.user,
            flow.business_id,
            {"query": "Pepsi", "semantic_query": "Pepsi drinks"},
            provider,
            request,
            None,
        )
    assert caught.value.reason == "relevance"


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "water", "browse": True},
        {"browse": False},
        {"query": "water", "browse": "false"},
        {"query": "water", "browse": 0},
        {"semantic_query": "drinkable water", "search_queries": ["water"]},
        {"query": 7},
        {"private-token-key": "private-token-value"},
    ],
)
def test_invalid_search_arguments_are_rejected_without_logging_values(
    flow, caplog, arguments
):
    import logging

    with caplog.at_level(logging.INFO, logger="app.services.owner_chat"):
        assert (
            owner_chat._validated_provider_arguments(
                flow.executor, "product_search", arguments
            )
            is None
        )
    assert "owner_chat_tool_arguments_invalid" in caplog.text
    assert "private-token-key" not in caplog.text
    assert "private-token-value" not in caplog.text
    assert "drinkable water" not in caplog.text


def test_operational_planner_receives_bounded_completed_context(flow, monkeypatch):
    history = tuple(ProviderMessage("owner", str(i)) for i in range(9))
    monkeypatch.setattr(
        owner_chat, "_load_provider_business", lambda *_: SimpleNamespace()
    )
    monkeypatch.setattr(owner_chat, "_provider_profile", lambda *_: PROFILE)
    monkeypatch.setattr(owner_chat, "_provider_context", lambda *_: (None, history))
    built = REAL_BUILD_OPERATIONAL_REQUEST(
        flow.session,
        flow.business_id,
        flow.owner.id,
        Settings(_env_file=None),
        flow.definitions,
    )
    assert built.request.messages == history[-5:]


@pytest.mark.parametrize(
    "question,family,label",
    [
        ("how much we have of PEPSI 330ML", "inventory", "Inventory"),
        ("sales", "sales", "Sales"),
        ("latest revenue", "financial", "Financial reporting"),
    ],
)
@pytest.mark.parametrize("approval", ["missing", "expired", "revoked", "valid"])
def test_empty_tool_registry_preserves_the_specific_capability_gate(
    flow, monkeypatch, question, family, label, approval
):
    from app.services import capabilities

    flow.owner.content = question
    business = SimpleNamespace(id=flow.business_id, default_language="en")
    original_get = flow.session.get
    monkeypatch.setattr(
        flow.session,
        "get",
        lambda model, key: business if model is Business else original_get(model, key),
    )
    flow.executor.available_definitions = lambda *_args: ()
    monkeypatch.setattr(
        owner_chat, "OperationalToolExecutor", lambda *_args: flow.executor
    )

    def event(session, source, requested_family):
        assert source is flow.source and requested_family == family
        if approval == "missing":
            return None
        return SimpleNamespace(
            approval_type="revoked" if approval == "revoked" else "full",
            expires_at=NOW + timedelta(seconds=-1 if approval == "expired" else 60),
        )

    monkeypatch.setattr(capabilities, "latest_event", event)
    saved = []
    monkeypatch.setattr(owner_chat, "_persist_result", lambda *args: saved.append(args))
    provider, calls = offline_provider()
    assert owner_chat._generate_claimed_turn(
        flow.session,
        flow.business_id,
        flow.claim,
        flow.user,
        provider,
        Settings(_env_file=None),
        object(),
    )
    answer = saved[0][3].reply
    assert label in answer
    assert (
        "has not been approved"
        if approval in {"missing", "revoked"}
        else "needs revalidation or renewed approval"
        if approval == "expired"
        else "is currently unavailable"
    ) in answer
    assert calls == flow.reads == []
    assert saved[0][4:6] == (None, None)


def test_semantic_search_reports_expired_catalogue_approval_without_a_source_read(
    flow, monkeypatch
):
    from app.services import catalogue_quality

    flow.definitions = ()
    flow.owner.content = "drinkable water"
    monkeypatch.setattr(
        catalogue_quality,
        "latest_event",
        lambda *_args: SimpleNamespace(
            approval_type="partial", expires_at=NOW - timedelta(seconds=1)
        ),
    )
    provider, calls = offline_provider(
        {
            "decision": "final",
            "semantic_operation": "product_search",
            "reply": "Here are water products.",
        }
    )
    result, usage = run(flow, provider)
    assert "Catalogue approval has expired" in result.reply
    assert "catalogue-only approval" in result.reply
    assert "Specify a product" not in result.reply
    assert flow.reads == [] and len(calls) == 1 and usage.total_tokens == 180


@pytest.mark.parametrize("use_budget", [False, True])
@pytest.mark.parametrize("reported_usage", [False, True])
@pytest.mark.parametrize("condition", ["expired", "current", "registered_tools"])
def test_rejected_plan_reports_only_verified_expired_approval_and_keeps_usage(
    flow, monkeypatch, use_budget, condition, reported_usage
):
    from app.services import catalogue_quality

    if condition != "registered_tools":
        flow.definitions = ()
    monkeypatch.setattr(
        catalogue_quality,
        "latest_event",
        lambda *_args: SimpleNamespace(
            approval_type="partial",
            expires_at=NOW + timedelta(seconds=60 if condition == "current" else -1),
        ),
    )
    provider, calls = offline_provider(
        {
            "decision": "final",
            "semantic_operation": "product_search",
            "entity_kind": "product",
            "entity_query": "water",
            "reply": "Unvalidated products must never be shown.",
        }
    )
    monkeypatch.setattr(provider, "estimate_input_tokens", lambda _request: 17)
    if not reported_usage:
        original = provider.transport

        def unreported(request):
            response = original.handle_request(request)
            body = response.json()
            body.pop("usageMetadata", None)
            return httpx.Response(200, json=body)

        provider.transport = httpx.MockTransport(unreported)
    charge = 180 if reported_usage else 17 + 512
    budget, events = usage_budget(flow, monkeypatch) if use_budget else (None, [])
    if condition == "expired":
        result, usage = run(flow, provider, budget)
        assert "Catalogue approval has expired" in result.reply
        assert "Unvalidated products" not in result.reply
        assert usage.total_tokens == result.usage.total_tokens == charge
    else:
        with pytest.raises(OwnerChatProviderInvalidResponse):
            run(flow, provider, budget)
    assert flow.reads == [] and len(calls) == 1
    if use_budget:
        assert budget.usage.total_tokens == charge and budget.calls == len(events) == 1
