"""October 17 regressions: synthetic tenants, HTTP mocks and no live writes."""

import socket
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from app.agent.mapping_provider import MappingGeneration, complete_category_joins
from app.agent.owner_chat_provider import (
    GeminiOwnerChatProvider,
    OwnerChatRequest,
    ProviderMessage,
    TokenUsage,
)
from app.core.exceptions import ApplicationError
from app.database.models import ChatGenerationState, OperationalDataSourceStatus
from app.integrations.discovery import SourceMappingError
from app.integrations.mapped_products import validate_mapping
from app.schemas.source_mapping import (
    CategoryMapping,
    DiscoveredColumn,
    DiscoveredObject,
    DiscoveredRelationship,
    JoinPair,
    MappingProposal,
    ProductMapping,
    SchemaDiscovery,
)
from app.services import data_sources, owner_chat, source_mapping
from sqlalchemy import Engine
from test_catalogue_chat import NOW, PROFILE, offline_provider, usage_budget
from test_owner_chat_state import complete, new_turn, product_plan, run, search
from test_owner_chat_state import system as system


@pytest.fixture(autouse=True)
def no_live_io(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Regression tests must not connect to real databases or providers")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(Engine, "connect", denied)


CASES = [
    ("can you give me lipton products?", "lipton"),
    ("give me all available lipton products", "lipton"),
    ("list lipton", "lipton"),
    ("what we have lipton", "lipton"),
    ("list pepsi dier", "pepsi dier"),
    ("what we have dairy", "dairy"),
    ("what we have diary", "diary"),
    ("what we have chips", "chips"),
    ("show me Aurora products", "Aurora"),
]


@pytest.mark.parametrize("message,term", CASES)
def test_explicit_lookup_never_becomes_unverified_conversation(system, message, term):
    new_turn(system, message)
    provider, calls = offline_provider(
        {"decision": "final", "semantic_operation": "conversation"},
        {"status": "ambiguous", "candidate_references": ["0", "1"]},
    )
    result, usage = run(system, provider)
    expected_calls = 2 if term == "Aurora" else 1
    assert len(calls) == expected_calls and usage.total_tokens == 180 * expected_calls
    assert len(system.reads) == 1 and system.reads[0]["query"] == term
    assert result.operational_presentation.capability == "products"
    if term == "Aurora":
        assert "1. Aurora Small\n2. Aurora Large" in result.reply
        assert "Stock and prices are unknown" in result.reply
    else:
        assert "No matching" in result.reply
        assert "knowledge base" not in result.reply


@pytest.mark.parametrize("message,term", CASES)
def test_unhealthy_source_does_not_dispatch_a_model_or_claim_empty_catalogue(
    system, monkeypatch, message, term
):
    system.source.status = OperationalDataSourceStatus.UNHEALTHY
    system.source.failure_code = "catalogue_value_invalid"
    new_turn(system, message)
    saved = []
    monkeypatch.setattr(owner_chat, "_persist_result", lambda *args: saved.append(args))
    provider, calls = offline_provider()
    assert owner_chat._generate_claimed_turn(
        system.session,
        system.business_id,
        system.claim,
        system.user,
        provider,
        system.settings,
        object(),
    )
    assert calls == [] and system.reads == []
    assert "not ready" in saved[0][3].reply
    assert "does not mean" in saved[0][3].reply
    assert saved[0][4:6] == (None, None)  # No reservation without generation.


def test_yes_after_unhealthy_catalogue_request_preserves_truthful_failure(
    system, monkeypatch
):
    new_turn(system, "what we have dairy")
    complete(system, SimpleNamespace(reply="Review source validation."))
    system.source.status = OperationalDataSourceStatus.UNHEALTHY
    new_turn(system, "yes")
    saved = []
    monkeypatch.setattr(owner_chat, "_persist_result", lambda *args: saved.append(args))
    provider, calls = offline_provider()
    owner_chat._generate_claimed_turn(
        system.session,
        system.business_id,
        system.claim,
        system.user,
        provider,
        system.settings,
        object(),
    )
    assert calls == [] and "not ready" in saved[0][3].reply


@pytest.mark.parametrize(
    "message",
    [
        "hi",
        "hi",
        "list opening hours",
        "show me delivery policy",
        "give me advice about products",
        "what are sales today?",
    ],
)
def test_non_catalogue_intents_remain_with_existing_router(message):
    assert not owner_chat._is_catalogue_lookup_request(message)


def test_changed_intent_does_not_reuse_previous_products(system):
    search(system, "Aurora")
    new_turn(system, "list Boreal")
    provider, _ = offline_provider(
        {"decision": "final", "semantic_operation": "knowledge"},
        {"status": "ambiguous", "candidate_references": ["0", "1"]},
    )
    result, _ = run(system, provider)
    assert system.reads[-1]["query"] == "Boreal"
    assert "Boreal Red" in result.reply and "Aurora Small" not in result.reply


def test_source_lookup_failure_is_not_knowledge_fallback(system, monkeypatch):
    new_turn(system, "list Aurora")

    def unavailable(*args, **kwargs):
        raise SourceMappingError("mapping_source_unavailable")

    monkeypatch.setattr("app.tools.operational.execute_product_search", unavailable)
    provider, calls = offline_provider(product_plan("Aurora"))
    result, _ = run(system, provider)
    assert len(calls) == 1
    assert "No matching" not in result.reply and "knowledge base" not in result.reply
    assert (
        "couldn't search" in result.reply.lower()
        and "does not mean" in result.reply.lower()
    )


@pytest.fixture
def composite():
    def obj(oid, fields, keys):
        return DiscoveredObject(
            object_id=oid,
            schema_name="fixture",
            name="object_" + oid,
            columns=tuple(
                DiscoveredColumn(name=f, kind="text", nullable=False) for f in fields
            ),
            unique_keys=keys,
        )

    discovery = SchemaDiscovery(
        engine="sqlserver",
        source_fingerprint="a" * 64,
        objects=(
            obj("o1", ("id", "label", "group", "segment"), (("id",),)),
            obj("o2", ("parent", "key", "caption"), (("parent", "key"),)),
        ),
    )
    partial = CategoryMapping(
        object_id="o2",
        label_column="caption",
        joins=(JoinPair(product_column="segment", related_column="key"),),
    )
    mapping = ProductMapping(
        capability="products",
        object_id="o1",
        key_columns=("id",),
        name_columns=("label",),
        categories=(partial,),
    )
    full = partial.model_copy(
        update={
            "joins": (
                *partial.joins,
                JoinPair(product_column="group", related_column="parent"),
            )
        }
    )
    approved = mapping.model_copy(update={"categories": (full,)})
    proposal = MappingProposal(
        mapping=mapping, rationale="Review source semantics", uncertainties=()
    )
    return discovery, approved, proposal


@pytest.mark.parametrize("authority", ["approval", "foreign_key"])
def test_composite_join_completion_requires_authority(composite, authority):
    discovery, approved, proposal = composite
    before = proposal.model_dump()
    if authority == "foreign_key":
        discovery = discovery.model_copy(
            update={
                "relationships": (
                    DiscoveredRelationship(
                        source_object_id="o1",
                        source_columns=("group", "segment"),
                        target_object_id="o2",
                        target_columns=("parent", "key"),
                    ),
                )
            }
        )
        approved = None
    corrected, notes = complete_category_joins(proposal, discovery, approved)
    validate_mapping(corrected.mapping, discovery)
    assert len(corrected.mapping.categories[0].joins) == 2 and notes
    assert proposal.model_dump() == before


def test_composite_completion_cannot_bypass_the_join_contract_bound(composite):
    discovery, _, proposal = composite
    target = discovery.objects[1].model_copy(
        update={
            "unique_keys": (("key", "parent", "region", "branch", "tenant"),),
            "columns": (
                *discovery.objects[1].columns,
                *(
                    DiscoveredColumn(name=name, kind="text", nullable=False)
                    for name in ("region", "branch", "tenant")
                ),
            ),
        }
    )
    discovery = discovery.model_copy(
        update={
            "objects": (discovery.objects[0], target),
            "relationships": (
                DiscoveredRelationship(
                    source_object_id="o1",
                    target_object_id="o2",
                    source_columns=("segment", "group", "id", "id", "id"),
                    target_columns=("key", "parent", "region", "branch", "tenant"),
                ),
            ),
        }
    )
    corrected, notes = complete_category_joins(proposal, discovery)
    assert not notes and corrected == proposal
    with pytest.raises(SourceMappingError, match="mapping_category_join_not_unique"):
        validate_mapping(corrected.mapping, discovery)


@pytest.mark.parametrize(
    "case",
    [
        "no_authority",
        "different_product_key",
        "different_scope",
        "contradiction",
        "ambiguous",
    ],
)
def test_unproved_composite_join_remains_rejected(composite, case):
    discovery, approved, proposal = composite
    if case == "no_authority":
        approved = None
    if case == "different_product_key":
        approved = approved.model_copy(update={"key_columns": ("group",)})
    if case == "different_scope":
        from app.schemas.source_mapping import ProductRowScope

        approved = approved.model_copy(
            update={"row_scopes": (ProductRowScope(column="group", value="tenant2"),)}
        )
    if case in {"contradiction", "ambiguous"}:
        joins = (
            JoinPair(product_column="id", related_column="parent"),
            JoinPair(
                product_column="segment" if case == "ambiguous" else "group",
                related_column="key",
            ),
        )
        altered = approved.categories[0].model_copy(update={"joins": joins})
        approved = approved.model_copy(
            update={
                "categories": (*approved.categories, altered)
                if case == "ambiguous"
                else (altered,)
            }
        )
    corrected, notes = complete_category_joins(proposal, discovery, approved)
    assert not notes
    with pytest.raises(SourceMappingError, match="mapping_category_join_not_unique"):
        validate_mapping(corrected.mapping, discovery)


@pytest.mark.parametrize(
    "initial,failure,expected",
    [
        ("ACTIVE", "catalogue_value_invalid", "UNHEALTHY"),
        ("UNHEALTHY", "catalogue_value_invalid", "UNHEALTHY"),
        ("UNHEALTHY", None, "VALIDATED"),
        ("ACTIVE", None, "ACTIVE"),
    ],
)
def test_validation_status_is_truthful_and_does_not_approve_or_activate(
    system, composite, monkeypatch, initial, failure, expected
):
    system.source.status = OperationalDataSourceStatus(initial)
    adapter = Mock()
    adapter.discovery, adapter.mapping, _ = composite
    adapter.connector.discover.return_value = adapter.discovery
    if failure:
        adapter.validate_results.side_effect = SourceMappingError(failure)
    monkeypatch.setattr(source_mapping, "require_open_admission", lambda: None)
    monkeypatch.setattr(source_mapping, "_source", lambda *args, **kw: system.source)
    monkeypatch.setattr(source_mapping, "mapped_source", lambda *args: adapter)
    monkeypatch.setattr(data_sources, "_response", lambda source, *args: source)
    source_mapping.validate_mapped_source(
        system.session, system.user, system.business_id, system.source.id, object()
    )
    assert system.source.status.value == expected
    assert system.source.failure_code == failure
    adapter.connector.discover.assert_called_once_with(object_ids={"o1", "o2"})


@pytest.mark.parametrize(
    "mode",
    [
        "conversation",
        "operational",
        "operational_synthesis",
        "category_resolution",
        "preference_resolution",
        "grounded",
    ],
)
def test_flash_lite_minimal_thinking_preserves_output_allowance(mode):
    from app.agent.owner_chat_provider import OwnerChatRequest, ProviderMessage
    from test_catalogue_chat import NOW, PROFILE

    provider = GeminiOwnerChatProvider(
        api_key="unused-offline", model="gemini-3.1-flash-lite", timeout_seconds=2
    )
    request = OwnerChatRequest(
        profile=PROFILE,
        knowledge=(),
        messages=(ProviderMessage("owner", "hi"),),
        requested_at=NOW,
        mode=mode,
    )
    config = provider._request_payload(request)["generationConfig"]
    assert config["thinkingConfig"]["thinkingLevel"] == (
        "MINIMAL"
        if mode in {"conversation", "operational", "category_resolution"}
        else "LOW"
    )
    assert config["maxOutputTokens"] == request.max_output_tokens == 512


def test_first_and_repeated_greeting_generate_once_each_without_catalogue(
    system, monkeypatch
):
    saved = []
    monkeypatch.setattr(owner_chat, "_persist_result", lambda *args: saved.append(args))
    for _ in range(2):
        new_turn(system, "hi")
        usage_budget(system, monkeypatch)
        monkeypatch.setattr(
            owner_chat,
            "_build_conversation_request",
            lambda *args: owner_chat._PreparedTurn(
                request=OwnerChatRequest(
                    profile=PROFILE,
                    knowledge=(),
                    messages=(ProviderMessage("owner", "hi"),),
                    requested_at=NOW,
                    mode="conversation",
                ),
                business=system.business,
                has_usable_evidence=True,
            ),
        )
        provider, calls = offline_provider(
            {"reply": "Hello! How can I help?", "requires_business_knowledge": False}
        )
        owner_chat._generate_claimed_turn(
            system.session,
            system.business_id,
            system.claim,
            system.user,
            provider,
            system.settings,
            None,
        )
        assert len(calls) == 1 and saved[-1][3].reply.startswith("Hello")
        complete(system, saved[-1][3])
    assert not system.reads


def test_truncated_greeting_is_not_retried_and_finalizes_hold_and_claim(
    system, monkeypatch
):
    new_turn(system, "hi")
    system.owner.generation_claim_token = system.claim.token
    usage_budget(system, monkeypatch)
    monkeypatch.setattr(
        owner_chat,
        "_build_conversation_request",
        lambda *args: owner_chat._PreparedTurn(
            request=OwnerChatRequest(
                profile=PROFILE,
                knowledge=(),
                messages=(ProviderMessage("owner", "hi"),),
                requested_at=NOW,
                mode="conversation",
            ),
            business=system.business,
            has_usable_evidence=True,
        ),
    )
    attempts, settlements = [], []

    def truncated(request):
        attempts.append(request)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "MAX_TOKENS",
                        "content": {"parts": [{"text": '{"reply":'}]},
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 1526,
                    "candidatesTokenCount": 496,
                    "totalTokenCount": 2022,
                },
            },
        )

    provider = GeminiOwnerChatProvider(
        api_key="offline",
        model="gemini-3.1-flash-lite",
        timeout_seconds=2,
        transport=httpx.MockTransport(truncated),
    )
    monkeypatch.setattr(
        owner_chat, "reconcile_ai_usage", lambda *args, **kw: settlements.append(kw)
    )
    with pytest.raises(ApplicationError) as caught:
        owner_chat._generate_claimed_turn(
            system.session,
            system.business_id,
            system.claim,
            system.user,
            provider,
            system.settings,
            None,
        )
    assert caught.value.error_code == "assistant_invalid_response"
    assert len(attempts) == len(settlements) == 1
    assert settlements[0]["outcome"] == "reported_failure"
    assert settlements[0]["usage"].total_tokens == 2022
    assert system.owner.generation_state is ChatGenerationState.FAILED
    assert system.owner.generation_claim_token is None


@pytest.mark.parametrize(
    "prior_state", ["absent", "stale_source", "stale_schema", "current"]
)
def test_proposal_retains_evidence_and_requires_current_approval_authority(
    composite, system, monkeypatch, prior_state
):
    discovery, approved, proposal = composite

    class ProposalSession:
        def add(self, row):
            row.id = uuid.uuid4()

        def flush(self):
            pass

        def commit(self):
            pass

        def refresh(self, row):
            pass

        def scalar(self, statement):
            return 1

        def execute(self, *args):
            return SimpleNamespace(
                one=lambda: SimpleNamespace(reservation_id=uuid.uuid4())
            )

    monkeypatch.setattr(source_mapping, "require_open_admission", lambda: None)
    monkeypatch.setattr(source_mapping, "_source", lambda *args, **kw: system.source)
    monkeypatch.setattr(source_mapping, "_existing_request", lambda *args: None)
    prior = (
        None
        if prior_state == "absent"
        else SimpleNamespace(
            approved_mapping=approved.model_dump(mode="json"),
            discovery={
                "source_fingerprint": "b" * 64
                if prior_state == "stale_source"
                else discovery.source_fingerprint
            },
            schema_fingerprint="f" * 64
            if prior_state == "stale_schema"
            else discovery.schema_fingerprint,
        )
    )
    monkeypatch.setattr(source_mapping, "latest_approved", lambda *args: prior)
    monkeypatch.setattr(
        source_mapping,
        "connector_for",
        lambda *args: SimpleNamespace(discover=lambda: discovery),
    )
    monkeypatch.setattr(
        source_mapping, "load_full_access_business", lambda *args: system.business
    )
    settled = []
    monkeypatch.setattr(
        source_mapping, "reconcile_ai_usage", lambda *args, **kw: settled.append(kw)
    )
    provider = SimpleNamespace(
        estimate_input_tokens=lambda d: 100,
        propose=lambda d: MappingGeneration(
            proposal=proposal,
            usage=TokenUsage(100, 80, 180, authoritative=True),
            provider="gemini",
            model="offline",
        ),
    )
    review = source_mapping.propose_mapping(
        ProposalSession(),
        system.user,
        system.business_id,
        system.source.id,
        uuid.uuid4(),
        object(),
        provider,
    )
    assert review.version == 2 and review.approved_mapping is None
    if prior_state == "current":
        assert review.status == "review" and review.failure_code is None
        validate_mapping(review.proposal.mapping, discovery)
        assert review.validation_notes
    else:
        assert review.status == "failed"
        assert review.failure_code == "mapping_category_join_not_unique"
        assert review.proposal == proposal
    assert len(settled) == 1 and settled[0]["usage"].total_tokens == 180
    assert settled[0]["outcome"] == (
        "completed" if prior_state == "current" else "reported_failure"
    )


@pytest.mark.parametrize(
    "question,operation,entity,expected",
    [
        ("prices", "product_price", None, "Pricing"),
        ("sales", "sales_summary", None, "Sales"),
        ("give me the latest sales", "sales_summary", None, "Sales"),
        ("how much do we have", "inventory_list", None, "Inventory"),
        (
            "how much we have of PEPSI 330ML",
            "inventory_product",
            "PEPSI 330ML",
            "Inventory",
        ),
    ],
)
def test_unapproved_operational_requests_never_search_catalogue(
    system, question, operation, entity, expected
):
    search(system)
    reads_before = len(system.reads)
    new_turn(system, question)
    plan = {
        "decision": "unavailable" if operation != "product_price" else "final",
        "semantic_operation": operation,
    }
    if operation != "product_price":
        plan["reply"] = "An unsafe invented answer: 999"
    if entity:
        plan.update(entity_kind="product", entity_query=entity)
    provider, _ = offline_provider(plan)
    result, _ = run(system, provider)
    assert len(system.reads) == reads_before
    assert expected in result.reply and "not been approved" in result.reply
    assert "999" not in result.reply
    assert system.owner.operational_clarification["state_kind"] == "discovery"


def test_unfamiliar_unsupported_intent_asks_instead_of_searching(system):
    new_turn(system, "Help with an unfamiliar business operation")
    provider, _ = offline_provider(
        {
            "decision": "unavailable",
            "semantic_operation": "unsupported",
            "reply": "I need more detail.",
        }
    )
    result, _ = run(system, provider)
    assert not system.reads
    assert "Which business information" in result.reply
    assert "Inventory access" not in result.reply
