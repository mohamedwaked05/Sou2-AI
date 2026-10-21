"""Phase 1 contracts and real orchestration, with in-memory persistence and HTTP.

Run with --confcutdir=tests/offline. No connection, migration or live provider
is allowed. Source retrieval and accounting persistence are mocked boundaries;
request construction, context loading, validation, tool authorization/audit and
catalogue response generation execute their production implementations.
"""

import json
import socket
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from app.agent.owner_chat_provider import (
    OwnerChatProviderInvalidResponse,
    OwnerChatProviderTimeout,
    OwnerChatRequest,
    OwnerChatResult,
    ProviderMessage,
    ProviderToolDefinition,
    _gemini_operational_response_schema,
    _OperationalStructuredResult,
)
from app.core.config import Settings
from app.core.exceptions import ApplicationError
from app.database.models import (
    Business,
    BusinessMembership,
    BusinessStatus,
    ChatGenerationState,
    ChatMessageRole,
    MembershipPermission,
    OperationalDataSourceConfig,
    OperationalDataSourceStatus,
    OwnerChatMessage,
    OwnerConversation,
    ToolCallLog,
    ToolCallStatus,
)
from app.integrations.mapped_products import MappedProductSource
from app.schemas.operational import (
    InventoryQuery,
    PendingInventoryClarification,
    ProductResolutionCandidate,
)
from app.schemas.owner_chat import OwnerMessageRequest
from app.schemas.owner_chat_state import ProductPresentation
from app.schemas.source_mapping import CatalogueProduct
from app.services import owner_chat, source_mapping
from app.services.businesses import load_full_access_business
from app.tools import operational
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import (
    AsBoolean,
    BinaryExpression,
    BindParameter,
    BooleanClauseList,
    False_,
    Grouping,
    Null,
    True_,
)
from test_catalogue_chat import NOW, PROFILE, catalogue, offline_provider, usage_budget


@pytest.fixture(autouse=True)
def offline_connections_only(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Phase 1 tests cannot connect to a network or database")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(Engine, "connect", forbidden)


def presentation(intent="discovery", reference="current", identifiers=()):
    return dict(intent=intent, reference=reference, identifiers=list(identifiers))


def product_plan(query, **changes):
    return {
        "decision": "tool",
        "semantic_operation": "product_search",
        "tool_name": "product_search",
        "arguments": {"query": query},
        "product_presentation": presentation(),
        **changes,
    }


class StateSession:
    """Evaluate the small SQLAlchemy SELECT subset used by these real loaders.

    Unknown expressions fail the test. Scope predicates are evaluated, rather
    than returning whichever fixture a mocked loader would choose.
    """

    def __init__(self):
        self.rows = {}
        self.added = []
        self.commits = 0
        self.statements = []

    def add(self, row):
        self.rows.setdefault(type(row), []).append(row)
        self.added.append(row)

    def get(self, model, key):
        return next((r for r in self.rows.get(model, ()) if r.id == key), None)

    def evaluate(self, expression, env):
        if isinstance(expression, BindParameter):
            return expression.value
        if isinstance(expression, Null):
            return None
        if isinstance(expression, (True_, False_)):
            return isinstance(expression, True_)
        if isinstance(expression, Grouping):
            return self.evaluate(expression.element, env)
        if isinstance(expression, AsBoolean):
            return bool(self.evaluate(expression.element, env))
        if isinstance(expression, BooleanClauseList):
            values = [self.evaluate(e, env) for e in expression.clauses]
            return all(values) if expression.operator is operators.and_ else any(values)
        if isinstance(expression, BinaryExpression):
            left, right = (
                self.evaluate(expression.left, env),
                self.evaluate(expression.right, env),
            )
            op = expression.operator
            if (
                op is operators.json_getitem_op
                or getattr(op, "opstring", None) == "->>"
            ):
                return (left or {}).get(right)
            if op is operators.is_:
                return left is right
            if op is operators.is_not:
                return left is not right
            if left is None or right is None:
                return False
            assert op in {
                operators.eq,
                operators.ne,
                operators.lt,
                operators.le,
                operators.gt,
                operators.ge,
                operators.in_op,
            }, op
            return op(left, right)
        if getattr(expression, "table", None) is not None:
            row = env.get(expression.table.name)
            return getattr(row, expression.key, None) if row is not None else None
        raise AssertionError(f"Unexpected SELECT expression: {type(expression)}")

    def select(self, statement):
        self.statements.append(statement)
        description = statement.column_descriptions[0]
        model = description["entity"]
        selected = []
        for row in self.rows.get(model, ()):
            env = {model.__tablename__: row}
            if model is Business:
                env[BusinessMembership.__tablename__] = next(
                    (
                        m
                        for m in self.rows.get(BusinessMembership, ())
                        if m.business_id == row.id
                    ),
                    None,
                )
            if statement.whereclause is None or self.evaluate(
                statement.whereclause, env
            ):
                selected.append(row)
        if statement._order_by_clauses and model is OwnerChatMessage:
            selected.sort(key=lambda r: r.sequence_number, reverse=True)
        if statement._limit_clause is not None:
            selected = selected[: statement._limit_clause.value]
        if description["expr"] is not model:
            selected = [getattr(row, description["name"]) for row in selected]
        return selected

    def scalar(self, statement):
        return next(iter(self.select(statement)), None)

    def scalars(self, statement):
        result = self.select(statement)
        return SimpleNamespace(all=lambda: result)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def refresh(self, _row):
        pass

    def flush(self):
        pass


@pytest.fixture
def system(monkeypatch):
    monkeypatch.setattr(owner_chat, "utc_now", lambda: NOW)
    session = StateSession()
    user = SimpleNamespace(id=uuid.uuid4())
    business = Business(
        id=uuid.uuid4(),
        name="Arbitrary fixture business",
        description="Offline",
        category="OTHER",
        governorate="Fixture",
        district="Fixture",
        city="Fixture",
        address_line="Fixture",
        timezone="UTC",
        default_language="en",
        status=BusinessStatus.ACTIVE,
        opening_days=[],
    )
    session.add(business)
    session.add(
        BusinessMembership(
            id=uuid.uuid4(),
            business_id=business.id,
            user_id=user.id,
            permission=MembershipPermission.FULL_ACCESS,
        )
    )
    conversation = OwnerConversation(
        id=uuid.uuid4(),
        business_id=business.id,
        creator_user_id=user.id,
        archived=False,
        next_turn_number=1,
    )
    session.add(conversation)
    source = OperationalDataSourceConfig(
        id=uuid.uuid4(),
        business_id=business.id,
        status=OperationalDataSourceStatus.ACTIVE,
        mapping_profile_key="discovered_products",
        mapping_profile_version=1,
        connection_profile_key="offline",
        updated_at=NOW,
    )
    session.add(source)
    items = [
        CatalogueProduct(
            external_product_id="A-1", names=("Aurora Small",), sku="AS", categories=()
        ),
        CatalogueProduct(
            external_product_id="A-2", names=("Aurora Large",), sku="AL", categories=()
        ),
        CatalogueProduct(
            external_product_id="B-1", names=("Boreal Red",), sku="BR", categories=()
        ),
        CatalogueProduct(
            external_product_id="B-2", names=("Boreal Blue",), sku="BB", categories=()
        ),
    ]
    adapter = Mock(spec=MappedProductSource)
    adapter.revision_version = 1
    adapter.discovery = SimpleNamespace(
        schema_fingerprint="a" * 64, source_fingerprint="b" * 64
    )
    adapter.enforced_query_timeout_seconds = 1
    revision = SimpleNamespace(version=1, schema_fingerprint="a" * 64)
    monkeypatch.setattr(source_mapping, "mapped_source", lambda *_: adapter)
    monkeypatch.setattr(source_mapping, "connector_for", lambda *_: None)
    monkeypatch.setattr(source_mapping, "latest_approved", lambda *_: revision)
    reads = []

    def retrieve(actual_adapter, query):
        assert actual_adapter is adapter
        reads.append(query.model_dump(exclude_none=True))
        if query.external_product_id is not None:
            matches = [
                p for p in items if p.external_product_id == query.external_product_id
            ]
        else:
            matches = [
                p
                for p in items
                if any(query.query.casefold() in n.casefold() for n in p.names)
            ]
        return catalogue(*matches).model_copy(
            update={"mapping_version": adapter.revision_version}
        )

    monkeypatch.setattr(operational, "execute_product_search", retrieve)
    settings = Settings(
        _env_file=None, tool_call_audit_hmac_secret="offline-audit-only"
    )
    profiles = SimpleNamespace(get_mapping=lambda *_: None)
    executor = operational.OperationalToolExecutor(session, profiles, settings)
    definitions = (
        ProviderToolDefinition(**executor.registry["product_search"].provider_schema()),
    )
    return SimpleNamespace(
        session=session,
        user=user,
        business=business,
        business_id=business.id,
        conversation=conversation,
        source=source,
        items=items,
        reads=reads,
        adapter=adapter,
        revision=revision,
        executor=executor,
        settings=settings,
        definitions=definitions,
        owner=None,
        claim=None,
    )


def new_turn(system, content):
    owners = [
        m
        for m in system.session.rows.get(OwnerChatMessage, ())
        if m.role is ChatMessageRole.OWNER
    ]
    sequence = max((m.sequence_number for m in owners), default=-1) + 2
    owner = OwnerChatMessage(
        id=uuid.uuid4(),
        conversation_id=system.conversation.id,
        sequence_number=sequence,
        role=ChatMessageRole.OWNER,
        content=content,
        generation_state=ChatGenerationState.PROCESSING,
        created_at=NOW,
        idempotency_key=str(uuid.uuid4()),
    )
    system.session.add(owner)
    system.owner = owner
    system.claim = owner_chat._Claim(owner.id, uuid.uuid4())
    return owner


def complete(system, result):
    system.owner.generation_state = ChatGenerationState.COMPLETED
    system.session.add(
        OwnerChatMessage(
            id=uuid.uuid4(),
            conversation_id=system.conversation.id,
            sequence_number=system.owner.sequence_number + 1,
            role=ChatMessageRole.ASSISTANT,
            content=result.reply,
            reply_to_message_id=system.owner.id,
            created_at=NOW,
        )
    )


def run(system, provider, budget=None):
    result, usage = owner_chat._run_operational_loop(
        system.session,
        system.business_id,
        system.claim,
        system.user,
        provider,
        system.settings,
        system.executor,
        system.definitions,
        budget=budget,
    )
    complete(system, result)
    return result, usage


def search(system, term="Aurora", content=None):
    new_turn(system, content or f"Show all {term} products")
    provider, calls = offline_provider(product_plan(term))
    result, usage = run(system, provider)
    return result, usage, calls


def test_independent_family_search_uses_current_terms_and_replaces_optional_context(
    system,
):
    search(system)
    first = system.owner
    assert first.operational_clarification["state_kind"] == "discovery"
    result, _, calls = search(system, "Boreal")
    assert "Boreal Red" in result.reply and "Aurora" not in result.reply
    assert '"pending_clarification"' not in json.dumps(calls[0]).replace('\\"', '"')
    assert "catalogue_context" in json.dumps(calls[0])
    assert system.reads[-1]["query"] == "Boreal"
    assert (
        system.owner.operational_clarification["candidates"][0]["external_product_id"]
        == "B-1"
    )
    assert (
        first.operational_clarification["candidates"][0]["external_product_id"] == "A-1"
    )


@pytest.mark.parametrize(
    "language,term,names,owner_text",
    [
        ("en", "Cinder", ("Cinder Short", "Cinder Long"), "List Cinder products"),
        ("ar", "نجمة", ("نجمة صغير", "نجمة كبير"), "اعرض منتجات نجمة"),
        ("en", "Vela", ("Vela Petit", "Vela Grand"), "farjini kel products Vela"),
    ],
)
def test_arbitrary_names_and_business_profiles_have_same_state_contract(
    system, language, term, names, owner_text
):
    system.business.default_language = language
    system.business.name = str(uuid.uuid4())
    system.items[:] = [
        CatalogueProduct(
            external_product_id=f"K-{i}", names=(name,), sku=f"S-{i}", categories=()
        )
        for i, name in enumerate(names)
    ]
    reply, _, _ = search(system, term, owner_text)
    assert f"1. {names[0]}\n2. {names[1]}" in reply.reply
    new_turn(system, names[1])
    provider, calls = offline_provider()
    selected, usage = run(system, provider)
    assert selected.reply.startswith(("Catalogue product: ", "منتج الكتالوج: "))
    assert "\n1." not in selected.reply and calls == [] and usage.total_tokens == 0
    assert selected.product_presentation.intent == "selection"
    assert system.reads[-1]["external_product_id"] == "K-1"
    assert [r for r in system.session.added if isinstance(r, ToolCallLog)][
        -1
    ].status is ToolCallStatus.SUCCESS


def test_exact_selection_then_identifier_then_independent_search(system):
    search(system)
    new_turn(system, "Aurora Large")
    provider, _ = offline_provider()
    selected, _ = run(system, provider)
    expiry = system.owner.operational_clarification["context"]["expires_at"]
    assert selected.reply.startswith("Catalogue product: Aurora Large")
    new_turn(system, "What's its SKU?")
    provider, calls = offline_provider(
        product_plan(
            "its", product_presentation=presentation("details", "selected", ("sku",))
        )
    )
    detail, _ = run(system, provider)
    assert (
        '"selected_catalogue_product_available":true'
        in calls[0]["systemInstruction"]["parts"][0]["text"]
    )
    assert "SKU: AL" in detail.reply and "ID:" not in detail.reply
    assert system.reads[-1]["external_product_id"] == "A-2"
    assert system.owner.operational_clarification["context"]["expires_at"] == expiry
    search(system, "Boreal")
    assert system.reads[-1]["query"] == "Boreal"


@pytest.mark.parametrize("message", ["hi", "مرحبا", "eh", "okay"])
def test_conversation_preserves_discovery_expiry_and_selection(
    system, monkeypatch, message
):
    search(system)
    prior_state = system.owner.operational_clarification
    new_turn(system, message)
    if owner_chat._is_clear_greeting(message):
        # Exercise top-level greeting routing and the real conversation builder;
        # only persistence/reservation I/O is replaced.
        saved = []
        usage_budget(system, monkeypatch)

        def persist(*args):
            saved.append(args)
            complete(system, args[3])

        monkeypatch.setattr(owner_chat, "_persist_result", persist)
        monkeypatch.setattr(
            owner_chat, "OperationalToolExecutor", lambda *_: system.executor
        )
        provider, calls = offline_provider(
            {
                "reply": "Offline generated conversation",
                "requires_business_knowledge": False,
            }
        )
        owner_chat._generate_claimed_turn(
            system.session,
            system.business_id,
            system.claim,
            system.user,
            provider,
            system.settings,
            object(),
        )
        assert (
            saved and len(calls) == 1 and "approved_tools" not in json.dumps(calls[0])
        )
    else:
        provider, calls = offline_provider(
            {"decision": "final", "semantic_operation": "conversation"},
            {
                "reply": "Offline generated acknowledgement",
                "requires_business_knowledge": False,
            },
        )
        run(system, provider)
        assert len(calls) == 2
    assert system.owner.operational_clarification == prior_state
    new_turn(system, "Aurora Small")
    provider, calls = offline_provider()
    selected, _ = run(system, provider)
    assert calls == [] and selected.reply.startswith("Catalogue product: Aurora Small")


def test_ambiguous_followup_creates_required_question_without_selection(system):
    search(system)
    expires_at = system.owner.operational_clarification["expires_at"]
    new_turn(system, "that one")
    provider, _ = offline_provider(
        product_plan(
            "that one",
            product_presentation=presentation("ambiguous_followup", "offered"),
        )
    )
    result, _ = run(system, provider)
    assert result.reply.startswith("Which catalogue product") and len(system.reads) == 1
    assert system.owner.operational_clarification["state_kind"] == "clarification"
    assert system.owner.operational_clarification["expires_at"] == expires_at
    new_turn(system, "eh")
    provider, _ = offline_provider(
        product_plan(
            "Aurora",
            pending_reply="unresolved",
            product_presentation=presentation("ambiguous_followup", "offered"),
        )
    )
    result, _ = run(system, provider)
    assert result.reply.startswith("Which catalogue product") and len(system.reads) == 1


def test_new_search_after_provider_failure_retains_valid_context(system):
    search(system)
    valid = system.owner.operational_clarification
    new_turn(system, "uncompleted message")
    provider, _ = offline_provider()

    def failed(_request):
        raise OwnerChatProviderTimeout()

    provider.generate = failed
    with pytest.raises(OwnerChatProviderTimeout):
        run(system, provider)
    system.owner.generation_state = ChatGenerationState.FAILED
    new_turn(system, "Show Boreal products")
    prepared = owner_chat._build_operational_request(
        system.session,
        system.business_id,
        system.owner.id,
        system.settings,
        system.definitions,
        user=system.user,
        executor=system.executor,
    )
    assert prepared.request.pending_clarification is None
    assert prepared.request.catalogue_context.candidates[0].names == ("Aurora Small",)
    provider, _ = offline_provider(product_plan("Boreal"))
    result, _ = run(system, provider)
    assert "Boreal" in result.reply and len(system.reads) == 2
    assert valid["candidates"][0]["external_product_id"] == "A-1"


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
def test_real_loader_rejects_scope_mapping_and_expiry_changes(system, field):
    search(system)
    state = system.owner.operational_clarification
    replacements = dict(
        source_updated_at=(NOW + timedelta(seconds=1)).isoformat(),
        expires_at=NOW.isoformat(),
        mapping_version=2,
        schema_fingerprint="c" * 64,
        source_fingerprint="d" * 64,
    )
    state[field] = replacements.get(field, str(uuid.uuid4()))
    new_turn(system, "Aurora Small")
    assert (
        owner_chat._pending_product_clarification(
            system.session,
            system.executor,
            system.user,
            system.business_id,
            system.owner.id,
        )
        is None
    )
    provider, _ = offline_provider(
        product_plan(
            "Aurora Small", product_presentation=presentation("selection", "offered")
        )
    )
    result, _ = run(system, provider)
    assert "expired or no longer valid" in result.reply and len(system.reads) == 1
    # An independent new request remains usable even with invalid prior metadata.
    search(system, "Boreal")
    assert system.reads[-1]["query"] == "Boreal"


def test_duplicate_names_require_current_identifier_without_merging(system):
    system.items[1] = system.items[1].model_copy(update={"names": ("Aurora Small",)})
    search(system)
    new_turn(system, "Aurora Small")
    provider, calls = offline_provider(
        product_plan(
            "Aurora Small", product_presentation=presentation("selection", "offered")
        )
    )
    result, _ = run(system, provider)
    assert "ID: A-1" in result.reply and "ID: A-2" in result.reply
    assert len(calls) == 1 and len(system.reads) == 1
    new_turn(system, "A-2")
    provider, calls = offline_provider()
    result, _ = run(system, provider)
    assert calls == [] and result.reply.startswith("Catalogue product: Aurora Small")
    assert system.reads[-1]["external_product_id"] == "A-2"


def test_real_tool_authorization_and_audit_remain_on_exact_selection(system):
    search(system)
    membership = system.session.rows[BusinessMembership][0]
    system.session.rows[BusinessMembership].remove(membership)
    with pytest.raises(ApplicationError):
        load_full_access_business(system.session, system.user, system.business_id)
    new_turn(system, "Aurora Small")
    provider, calls = offline_provider()
    with pytest.raises(ApplicationError):
        run(system, provider)
    pending = owner_chat._pending_product_clarification(
        system.session,
        system.executor,
        system.user,
        system.business_id,
        system.owner.id,
    )
    with pytest.raises(operational.ToolExecutionError) as caught:
        owner_chat._read_exact_catalogue_selection(
            system.session,
            system.executor,
            system.user,
            system.business_id,
            system.owner,
            pending,
        )
    assert caught.value.code == "authorization_denied"
    assert calls == [] and len(system.reads) == 1
    audits = [r for r in system.session.added if isinstance(r, ToolCallLog)]
    assert audits[-1].status is ToolCallStatus.DENIED


def test_real_terminal_replay_and_content_conflict(system):
    result, _, _ = search(system)
    original = system.owner
    body = OwnerMessageRequest(
        content=original.content, idempotency_key=original.idempotency_key
    )
    owner, replayed, claim = owner_chat._create_or_reuse_owner_message(
        system.session, system.conversation.id, body, system.settings
    )
    assert owner is original and replayed and claim is None
    replay = owner_chat._completed_turn(system.session, owner, replayed)
    assert replay.replayed and replay.assistant_message.content == result.reply
    assert len(system.reads) == 1
    with pytest.raises(ApplicationError) as caught:
        owner_chat._create_or_reuse_owner_message(
            system.session,
            system.conversation.id,
            OwnerMessageRequest(
                content="different", idempotency_key=body.idempotency_key
            ),
            system.settings,
        )
    assert caught.value.error_code == "idempotency_conflict"


def test_real_routing_retains_reservation_and_reported_usage(system, monkeypatch):
    new_turn(system, "Show all Aurora products")
    budget, events = usage_budget(system, monkeypatch)
    provider, calls = offline_provider(product_plan("Aurora"))
    _, usage = run(system, provider, budget)
    assert len(events) == len(calls) == 1 and usage.total_tokens == 180
    assert budget.usage.total_tokens == 180 and budget.reservation is not None
    assert events[0][1]["estimated_input_tokens"] == provider.estimate_input_tokens(
        owner_chat._build_operational_request(
            system.session,
            system.business_id,
            system.owner.id,
            system.settings,
            system.definitions,
            user=system.user,
            executor=system.executor,
        ).request
    )


def schema_accepts(schema, value, root):
    """Evaluate only the JSON-schema subset emitted in these planner fixtures."""
    if "$ref" in schema:
        definition = root
        for key in schema["$ref"].removeprefix("#/").split("/"):
            definition = definition[key]
        return schema_accepts(definition, value, root)
    if "enum" in schema and value not in schema["enum"]:
        return False
    if "anyOf" in schema and not any(
        schema_accepts(s, value, root) for s in schema["anyOf"]
    ):
        return False
    if schema.get("type") == "object" or "properties" in schema:
        if (
            not isinstance(value, dict)
            or not set(schema.get("required", ())) <= value.keys()
        ):
            return False
        properties = schema.get("properties", {})
        if (
            schema.get("additionalProperties") is False
            and not value.keys() <= properties.keys()
        ):
            return False
        return all(
            schema_accepts(properties[k], v, root)
            for k, v in value.items()
            if k in properties
        )
    if schema.get("type") == "string":
        return isinstance(value, str) and schema.get("minLength", 0) <= len(
            value
        ) <= schema.get("maxLength", 100000)
    if schema.get("type") == "null":
        return value is None
    if schema.get("type") == "array":
        return (
            isinstance(value, list)
            and len(value) <= schema.get("maxItems", 1000)
            and all(schema_accepts(schema["items"], v, root) for v in value)
        )
    return True


@pytest.mark.parametrize(
    "operation",
    [
        None,
        "preference",
        "product_search",
        "inventory_product",
        "inventory_category",
        "inventory_list",
        "sales_summary",
        "restocking",
    ],
)
@pytest.mark.parametrize(
    "action,semantic",
    [
        ("final", "conversation"),
        ("final", "unsupported"),
        ("tool", "product_search"),
        ("tool", "inventory_product"),
        ("set_preference", "preference"),
        ("clear_preference", "preference"),
    ],
)
@pytest.mark.parametrize(
    "transition",
    [None, "selection", "confirmation", "unresolved", "unrelated", "cancel", "replace"],
)
@pytest.mark.parametrize("quote", [None, "Search Boreal", "", "Different request"])
def test_exhaustive_schema_model_and_backend_transition_agreement(
    operation, action, semantic, transition, quote
):
    registry = operational.build_operational_tool_registry(timeout_seconds=1)
    request = OwnerChatRequest(
        profile=PROFILE,
        knowledge=(),
        requested_at=NOW,
        messages=(ProviderMessage("owner", "Search Boreal"),),
        mode="operational",
        tools=tuple(
            ProviderToolDefinition(**registry[name].provider_schema())
            for name in ("product_search", "current_inventory")
        ),
        pending_clarification={"operation": operation} if operation else None,
    )
    payload = dict(decision=action, semantic_operation=semantic)
    if semantic == "unsupported":
        payload["reply"] = "Unsupported operation"
    if action == "tool":
        payload.update(
            tool_name="product_search"
            if semantic == "product_search"
            else "current_inventory",
            arguments={"query": "Boreal"}
            if semantic == "product_search"
            else {"product_filter": "Boreal"},
        )
        if semantic == "product_search":
            payload["product_presentation"] = presentation()
        else:
            payload.update(entity_kind="product", entity_query="Boreal")
    if transition is not None:
        payload["pending_reply"] = transition
    if quote is not None:
        payload["pending_request"] = quote
    schema = _gemini_operational_response_schema(request)
    schema_valid = schema_accepts(schema, payload, schema)
    try:
        parsed = _OperationalStructuredResult.model_validate(
            payload,
            context={"operational_contract": True, "pending_operation": operation},
        )
        model_valid = True
    except ValidationError:
        model_valid = False
    assert model_valid == schema_valid
    result = OwnerChatResult(
        decision=action,
        semantic_operation=semantic,
        pending_reply=transition,
        pending_request=quote,
    )
    try:
        owner_chat._validate_pending_preference_reply(result, request)
        backend_valid = True
    except OwnerChatProviderInvalidResponse:
        backend_valid = False
    # Only the backend can check exact evidence against the current message.
    assert backend_valid == (schema_valid and quote != "Different request")
    if model_valid:
        assert parsed.pending_reply == transition


def test_rejected_transition_diagnostics_do_not_log_customer_content(caplog):
    request = OwnerChatRequest(
        profile=PROFILE,
        knowledge=(),
        requested_at=NOW,
        messages=(ProviderMessage("owner", "PRIVATE OWNER MESSAGE"),),
        mode="operational",
        pending_clarification={"operation": "inventory_product"},
    )
    result = OwnerChatResult(
        decision="final",
        semantic_operation="conversation",
        pending_reply="unrelated",
        pending_request="PRIVATE WRONG QUOTE",
    )
    with caplog.at_level("INFO"), pytest.raises(OwnerChatProviderInvalidResponse):
        owner_chat._validate_pending_preference_reply(result, request)
    assert "ungrounded_transition_evidence" in caplog.text
    assert "PRIVATE" not in caplog.text


def test_explicit_presentation_cannot_be_reinterpreted_by_owner_words():
    output = catalogue(
        CatalogueProduct(
            external_product_id="any",
            names=("List All Matches",),
            sku=None,
            categories=(),
        )
    )
    reply = owner_chat._catalogue_reply(
        output,
        "Show all products",
        presentation=ProductPresentation(intent="selection", reference="offered"),
    )
    assert (
        reply.startswith("Catalogue product: List All Matches") and "\n1." not in reply
    )


def test_independent_request_containing_an_offered_name_does_not_bind_old_identity(
    system,
):
    search(system)
    result, _, _ = search(
        system, "Aurora Small", "Search products matching Aurora Small"
    )
    assert system.reads[-1]["query"] == "Aurora Small"
    assert "external_product_id" not in system.reads[-1]
    assert result.reply.startswith("Matching catalogue products:")
    assert system.owner.operational_clarification["state_kind"] == "discovery"
    assert len(system.owner.operational_clarification["candidates"]) == 1


def test_required_clarification_allows_grounded_independent_search(system):
    new_turn(system, "Tell me about Aurora")
    provider, _ = offline_provider(
        product_plan("Aurora", product_presentation=presentation("details"))
    )
    result, _ = run(system, provider)
    assert result.reply.startswith("Which catalogue product")
    new_turn(system, "Search Boreal")
    provider, _ = offline_provider(
        product_plan(
            "Boreal", pending_reply="unrelated", pending_request="Search Boreal"
        )
    )
    result, _ = run(system, provider)
    assert "Boreal" in result.reply and "Aurora" not in result.reply
    assert system.reads[-1]["query"] == "Boreal"


@pytest.mark.parametrize(
    "legacy_request,kind",
    [("List Aurora products", "discovery"), ("Tell me about Aurora", "clarification")],
)
def test_legacy_metadata_normalizes_without_writing_old_messages(
    system, legacy_request, kind
):
    search(system)
    old_owner = system.owner
    state = old_owner.operational_clarification
    state.pop("state_kind")
    state.pop("presentation")
    state["owner_request"] = legacy_request
    unchanged = json.dumps(state, sort_keys=True)
    new_turn(system, "Find Boreal")
    prepared = owner_chat._build_operational_request(
        system.session,
        system.business_id,
        system.owner.id,
        system.settings,
        system.definitions,
        user=system.user,
        executor=system.executor,
    )
    assert (prepared.request.pending_clarification is None) == (kind == "discovery")
    assert json.dumps(old_owner.operational_clarification, sort_keys=True) == unchanged


def test_planned_selection_checks_fresh_exact_identity(system, monkeypatch):
    search(system)
    original = operational.execute_product_search

    def wrong_product(adapter, query):
        if query.external_product_id is not None:
            return catalogue(system.items[-1])
        return original(adapter, query)

    monkeypatch.setattr(operational, "execute_product_search", wrong_product)
    new_turn(system, "Choose Aurora Small")
    provider, _ = offline_provider(
        product_plan(
            "Aurora Small", product_presentation=presentation("selection", "offered")
        )
    )
    result, usage = run(system, provider)
    assert "no longer a unique catalogue product" in result.reply
    assert "Boreal" not in result.reply and usage.total_tokens == 180
    assert (
        system.owner.operational_clarification["operation"]
        == "catalogue_context_cleared"
    )


def test_rejected_current_message_evidence_preserves_context_and_reported_charge(
    system, monkeypatch
):
    new_turn(system, "Tell me about Aurora")
    provider, _ = offline_provider(
        product_plan("Aurora", product_presentation=presentation("details"))
    )
    run(system, provider)
    original = system.owner.operational_clarification
    new_turn(system, "Search Boreal")
    budget, events = usage_budget(system, monkeypatch)
    provider, calls = offline_provider(
        product_plan(
            "Boreal", pending_reply="unrelated", pending_request="Different request"
        )
    )
    with pytest.raises(OwnerChatProviderInvalidResponse) as caught:
        run(system, provider, budget)
    assert caught.value.reason == "invalid_pending_transition"
    assert caught.value.usage.total_tokens == budget.usage.total_tokens == 180
    assert len(events) == len(calls) == 1
    system.owner.generation_state = ChatGenerationState.FAILED
    new_turn(system, "Choose Aurora Small")
    prepared = owner_chat._build_operational_request(
        system.session,
        system.business_id,
        system.owner.id,
        system.settings,
        system.definitions,
        user=system.user,
        executor=system.executor,
    )
    assert prepared.request.pending_clarification["operation"] == "product_search"
    assert original["candidates"][0]["external_product_id"] == "A-1"


@pytest.mark.parametrize("message", ["eh", "yes", "تمام", "اي"])
def test_acknowledgement_cannot_supply_cancellation_or_unrelated_evidence(message):
    request = OwnerChatRequest(
        profile=PROFILE,
        knowledge=(),
        requested_at=NOW,
        messages=(ProviderMessage("owner", message),),
        mode="operational",
        pending_clarification={"operation": "product_search"},
    )
    for transition in ("cancel", "unrelated"):
        with pytest.raises(OwnerChatProviderInvalidResponse):
            owner_chat._validate_pending_preference_reply(
                OwnerChatResult(
                    decision="final",
                    semantic_operation="conversation",
                    pending_reply=transition,
                    pending_request=message,
                ),
                request,
            )


def test_inventory_context_and_independent_intent_survive_failed_turn(system):
    system.source.mapping_profile_key = "fake_store_minimarket"
    new_turn(system, "Find stock for Cinder")
    pending = PendingInventoryClarification(
        user_id=system.user.id,
        source_id=system.source.id,
        source_updated_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
        arguments=InventoryQuery(
            product_filter="Cinder", branch_external_id="old-location"
        ),
        candidates=(
            ProductResolutionCandidate(external_product_id="C-1", name="Cinder Round"),
            ProductResolutionCandidate(external_product_id="C-2", name="Cinder Square"),
        ),
        owner_request="Find stock for Cinder",
    )
    system.owner.operational_clarification = pending.model_dump(mode="json")
    complete(system, OwnerChatResult(reply="Which product?"))
    new_turn(system, "failed message")
    system.owner.generation_state = ChatGenerationState.FAILED
    new_turn(system, "Find stock for Delta")
    definitions = (
        ProviderToolDefinition(
            **system.executor.registry["current_inventory"].provider_schema()
        ),
    )
    prepared = owner_chat._build_operational_request(
        system.session,
        system.business_id,
        system.owner.id,
        system.settings,
        definitions,
        user=system.user,
        executor=system.executor,
    )
    assert prepared.request.pending_clarification["state_kind"] == "clarification"
    assert (
        prepared.request.pending_clarification["candidates"][0]["label"]
        == "Cinder Round"
    )
    provider, _ = offline_provider(
        dict(
            decision="tool",
            semantic_operation="inventory_product",
            entity_kind="product",
            entity_query="Delta",
            tool_name="current_inventory",
            arguments={"product_filter": "Delta"},
            pending_reply="unrelated",
            pending_request="Find stock for Delta",
        )
    )
    result = owner_chat._validated_generation(provider, prepared.request)
    command = owner_chat._backend_operational_command(
        result, system.executor, (), prepared.request
    )
    assert command.arguments["product_filter"] == "Delta"
    assert "branch_external_id" not in command.arguments
    assert system.reads == []


def test_business_switch_does_not_copy_catalogue_context_to_other_capability(system):
    search(system)
    old_business = system.business
    other_business = Business(
        id=uuid.uuid4(),
        name="Different tenant",
        description="Offline",
        category="OTHER",
        governorate="Fixture",
        district="Fixture",
        city="Fixture",
        address_line="Fixture",
        timezone="UTC",
        default_language="ar",
        status=BusinessStatus.ACTIVE,
        opening_days=[],
    )
    system.session.add(other_business)
    system.session.add(
        BusinessMembership(
            id=uuid.uuid4(),
            business_id=other_business.id,
            user_id=system.user.id,
            permission=MembershipPermission.FULL_ACCESS,
        )
    )
    other_conversation = OwnerConversation(
        id=uuid.uuid4(),
        business_id=other_business.id,
        creator_user_id=system.user.id,
        archived=False,
    )
    system.session.add(other_conversation)
    system.session.add(
        OperationalDataSourceConfig(
            id=uuid.uuid4(),
            business_id=other_business.id,
            status=OperationalDataSourceStatus.ACTIVE,
            mapping_profile_key="fake_store_minimarket",
            mapping_profile_version=1,
            connection_profile_key="other-offline",
            updated_at=NOW,
        )
    )
    system.business_id = other_business.id
    system.conversation = other_conversation
    new_turn(system, "اعرض المنتجات")
    definitions = (
        ProviderToolDefinition(
            **system.executor.registry["current_inventory"].provider_schema()
        ),
    )
    prepared = owner_chat._build_operational_request(
        system.session,
        system.business_id,
        system.owner.id,
        system.settings,
        definitions,
        user=system.user,
        executor=system.executor,
    )
    assert prepared.request.profile.name == "Different tenant"
    assert (
        prepared.request.catalogue_context is None
        and prepared.request.pending_clarification is None
    )
    assert not prepared.request.selected_catalogue_product_available
    assert (
        owner_chat._pending_product_clarification(
            system.session,
            system.executor,
            system.user,
            old_business.id,
            system.owner.id,
        )
        is None
    )


def test_actual_mapping_revision_change_invalidates_old_choices_but_allows_new_search(
    system,
):
    search(system)
    system.adapter.revision_version = system.revision.version = 2
    new_turn(system, "Select Aurora Small")
    prepared = owner_chat._build_operational_request(
        system.session,
        system.business_id,
        system.owner.id,
        system.settings,
        system.definitions,
        user=system.user,
        executor=system.executor,
    )
    assert prepared.request.catalogue_context.state_kind == "invalid_or_expired"
    assert prepared.request.pending_clarification is None
    result, _, _ = search(system, "Boreal")
    assert (
        "Boreal" in result.reply
        and system.owner.operational_clarification["mapping_version"] == 2
    )


def test_single_discovery_record_cannot_overflow_context_storage(system):
    huge = CatalogueProduct(
        external_product_id="x" * 4096,
        names=("ب" * 255, "ع" * 255, "ت" * 255),
        sku="s" * 128,
        categories=("ف" * 255, "ج" * 255, "ك" * 255),
    )
    system.items[:] = [huge]
    result, _, _ = search(system, "ب")
    encoded = json.dumps(
        system.owner.operational_clarification, ensure_ascii=False
    ).encode()
    assert len(encoded) <= owner_chat.CATALOGUE_CONTEXT_MAX_BYTES
    assert (
        "exceed the display limit" in result.reply
        or "تتجاوز النتائج حد العرض" in result.reply
    )
    assert system.items == [huge]


def test_singular_identifier_followup_to_one_offered_product_is_fresh_and_scoped(
    system,
):
    search(system, "Aurora Small")
    expiry = system.owner.operational_clarification["expires_at"]
    new_turn(system, "What's its SKU?")
    provider, _ = offline_provider(
        product_plan(
            "its", product_presentation=presentation("details", "offered", ("sku",))
        )
    )
    result, _ = run(system, provider)
    assert result.reply.startswith("Catalogue product: Aurora Small")
    assert "SKU: AS" in result.reply and "ID:" not in result.reply
    assert system.reads[-1]["external_product_id"] == "A-1"
    assert system.owner.operational_clarification["context"]["expires_at"] == expiry


def test_short_acknowledgement_cannot_revive_optional_operational_filters(system):
    search(system)
    new_turn(system, "eh")
    provider, _ = offline_provider(product_plan("Aurora"))
    with pytest.raises(OwnerChatProviderInvalidResponse) as caught:
        run(system, provider)
    assert caught.value.reason == "acknowledgement_is_not_operation"
    assert caught.value.usage.total_tokens == 180 and len(system.reads) == 1


def test_inventory_acknowledgement_cannot_select_a_candidate_named_yes(system):
    pending = PendingInventoryClarification(
        user_id=system.user.id,
        source_id=system.source.id,
        source_updated_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
        arguments=InventoryQuery(product_filter="arbitrary"),
        candidates=(
            ProductResolutionCandidate(external_product_id="Y", name="yes"),
            ProductResolutionCandidate(external_product_id="N", name="other"),
        ),
    )
    assert owner_chat._selected_pending_product("yes", pending) is None


def test_sales_question_survives_failed_turn_without_reviving_filters(system):
    new_turn(system, "Report sales")
    complete(system, OwnerChatResult(reply=owner_chat._SALES_CLARIFICATION_REPLY))
    new_turn(system, "failed message")
    system.owner.generation_state = ChatGenerationState.FAILED
    new_turn(system, "revenue for the previous completed month")
    assert owner_chat._pending_sales_clarification(
        system.session, system.owner.id, NOW, NOW
    )
    assert not owner_chat._pending_sales_clarification(
        system.session, system.owner.id, NOW + timedelta(minutes=16), NOW
    )
    assert not owner_chat._pending_sales_clarification(
        system.session, system.owner.id, NOW, NOW + timedelta(seconds=1)
    )
    assert all(
        "where" in str(statement).lower() for statement in system.session.statements
    )
    # A completed independent exchange replaces the question; failed ones do not.
    complete(system, OwnerChatResult(reply="Independent completed answer"))
    new_turn(system, "revenue")
    assert not owner_chat._pending_sales_clarification(
        system.session, system.owner.id, NOW, NOW
    )
