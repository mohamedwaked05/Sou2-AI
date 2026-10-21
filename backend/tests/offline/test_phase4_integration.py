"""HTTP -> auth -> routing -> approved source SQL -> rendering -> persistence.

JWTs, services, provider decoding, tools, mappings and presentation are real.
Gemini HTTP is controlled. Platform persistence/SQL functions are transactional
in-memory doubles; native constraints, roles and locking are not verified here.
"""

import copy
import json
import socket
import sys
import uuid
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from app.agent.owner_chat_provider import (
    GeminiOwnerChatProvider,
    get_owner_chat_provider,
)
from app.api.dependencies import run_security_record_cleanup
from app.api.v1.routes.owner_chat import router
from app.core import runtime as worker_runtime
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.runtime import job_contract
from app.core.security import create_access_token
from app.database.models import (
    AccountStatus,
    AIUsageReservation,
    Business,
    BusinessCategory,
    BusinessMembership,
    BusinessOpeningDay,
    ChatGenerationState,
    DefaultLanguage,
    MembershipPermission,
    OperationalDataSourceConfig,
    OperationalDataSourceStatus,
    OwnerChatMessage,
    OwnerConversation,
    OwnerConversationSummary,
    SourceMappingRevision,
    ToolCallLog,
    User,
)
from app.database.session import get_db_session
from app.integrations.profiles import (
    FAKE_STORE_MAPPING,
    ConnectionProfile,
    MappingProfileError,
    get_connection_profile_registry,
)
from app.services import ai_usage, conversations, owner_chat
from app.tools.operational import OperationalToolExecutor, ToolExecutionError
from app.worker import conversation_summary as memory
from fastapi import FastAPI
from fastapi.testclient import TestClient
from integration_sources import legacy_source, mapped_catalogue
from sqlalchemy import Engine
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BinaryExpression
from sqlalchemy.sql.functions import Function
from sqlalchemy.sql.selectable import ScalarSelect
from test_catalogue_chat import NOW, offline_provider
from test_owner_chat_state import StateSession, product_plan
from test_phase3_reliability import summary_row


class TransactionalPlatform(StateSession):
    """Evaluate production SELECTs and implement only named SQL-function contracts."""

    def __init__(self):
        super().__init__()
        self.now = NOW
        self.function_calls = []
        self.charges = {}
        self.snapshot = {}
        self.snapshots = []

    def add(self, row):
        if getattr(row, "id", None) is None:
            row.id = uuid.uuid4()
        for field in ("created_at", "updated_at"):
            if hasattr(row, field) and getattr(row, field) is None:
                setattr(row, field, NOW)
        if isinstance(row, OwnerChatMessage):
            row.generation_attempts = row.generation_attempts or 0
            row.citations = []
        if isinstance(row, OwnerConversation):
            row.title = row.title or "New conversation"
            row.next_turn_number = row.next_turn_number or 1
            row.channel = "owner_web"
            row.archived = row.archived or False
        super().add(row)

    def add_all(self, rows):
        for row in rows:
            self.add(row)

    def evaluate(self, expression, env):
        if (
            isinstance(expression, BinaryExpression)
            and expression.operator is operators.in_op
        ):
            return self.evaluate(expression.left, env) in self.evaluate(
                expression.right, env
            )
        if isinstance(expression, ScalarSelect):
            return self.select(expression.element)
        if isinstance(expression, Function) and expression.name == "clock_timestamp":
            return self.now
        return super().evaluate(expression, env)

    def select(self, statement):
        # The existing fixture evaluates tenant/membership predicates. Sort these
        # additional models before applying the production LIMIT.
        model = statement.column_descriptions[0]["entity"]
        assert model is not AIUsageReservation, (
            "Restricted runtime cannot SELECT the accounting table"
        )
        if model is SourceMappingRevision:
            self.rows[model] = sorted(
                self.rows.get(model, ()), key=lambda r: r.version, reverse=True
            )
        if model is OwnerConversation and statement._order_by_clauses:
            self.rows[model] = sorted(
                self.rows.get(model, ()),
                key=lambda r: (r.last_message_at or r.created_at, r.id),
                reverse=True,
            )
        return super().select(statement)

    def scalar(self, statement):
        if "clock_timestamp" in str(statement) and "FROM" not in str(statement):
            return self.now
        return super().scalar(statement)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.rollback()
        return False

    def execute(self, statement, parameters=None):
        if hasattr(statement, "column_descriptions"):
            assert len(statement.column_descriptions) == 2
            alias = statement.column_descriptions[1]["entity"]._aliased_insp.selectable
            join = statement.get_final_froms()[0]
            pairs = []
            messages = self.rows.get(OwnerChatMessage, ())
            for left in messages:
                for right in messages:
                    env = {OwnerChatMessage.__tablename__: left, alias.name: right}
                    if self.evaluate(statement.whereclause, env) and self.evaluate(
                        join.onclause, env
                    ):
                        pairs.append((left, right))
            pairs.sort(key=lambda pair: pair[0].sequence_number)
            return SimpleNamespace(all=lambda: pairs)
        if getattr(statement, "is_insert", False):
            assert statement.table.name == OwnerConversationSummary.__tablename__
            values = statement.compile().params
            if not any(
                row.conversation_id == values["conversation_id"]
                for row in self.rows.get(OwnerConversationSummary, ())
            ):
                self.add(
                    summary_row(
                        **{
                            key: value
                            for key, value in values.items()
                            if value is not None
                        }
                    )
                )
            return SimpleNamespace()
        assert parameters is not None
        sql = str(statement)
        self.function_calls.append((sql, parameters.copy()))
        if "sou2ai_admit_owner_chat_generation(" in sql:
            row = SimpleNamespace(admitted=True)
        elif "sou2ai_reserve_ai_usage(" in sql:
            message = self.get(OwnerChatMessage, parameters["message_id"])
            conversation = self.get(OwnerConversation, message.conversation_id)
            assert conversation.business_id == parameters["business_id"]
            assert message.generation_state == ChatGenerationState.PROCESSING
            assert message.generation_attempts == parameters["attempt"]
            assert not any(
                r.owner_message_id == message.id
                for r in self.rows.get(AIUsageReservation, ())
            )
            reservation = AIUsageReservation(
                id=uuid.uuid4(),
                business_id=conversation.business_id,
                owner_message_id=message.id,
                status="reserved",
                generation_attempt=parameters["attempt"],
                estimated_input_tokens=parameters["estimated_input"],
                max_output_tokens=parameters["max_output"],
                lease_expires_at=message.generation_claim_expires_at,
                window_end=NOW + timedelta(hours=1),
            )
            self.add(reservation)
            row = SimpleNamespace(
                reservation_id=reservation.id, reset_at=reservation.window_end
            )
        elif "sou2ai_reserve_conversation_summary_usage(" in sql:
            summary = self.get(OwnerConversationSummary, parameters["summary_id"])
            assert summary.generation_claim_token == parameters["claim_token"]
            assert not any(
                r.conversation_summary_id == summary.id
                and r.generation_attempt == summary.generation_attempts
                for r in self.rows.get(AIUsageReservation, ())
            )
            reservation = AIUsageReservation(
                id=uuid.uuid4(),
                business_id=summary.business_id,
                conversation_summary_id=summary.id,
                generation_attempt=summary.generation_attempts,
                status="reserved",
                estimated_input_tokens=parameters["estimated_input"],
                max_output_tokens=parameters["max_output"],
                lease_expires_at=summary.generation_claim_expires_at,
                window_end=NOW + timedelta(hours=1),
            )
            self.add(reservation)
            row = SimpleNamespace(
                reservation_id=reservation.id, reset_at=reservation.window_end
            )
        elif "sou2ai_lock_active_ai_usage_reservation(" in sql:
            reservation = self.get(AIUsageReservation, parameters["reservation_id"])
            deadline = parameters["finalization_deadline"]
            active = (
                reservation is not None
                and reservation.status == "reserved"
                and reservation.lease_expires_at > self.now
                and reservation.window_end > self.now
                and (deadline is None or deadline > self.now)
            )
            row = (
                SimpleNamespace(
                    lease_expires_at=reservation.lease_expires_at,
                    window_end=reservation.window_end,
                )
                if active
                else None
            )
        elif "sou2ai_summary_attempt_has_usage(" in sql:
            summary = self.get(OwnerConversationSummary, parameters["summary_id"])
            row = bool(
                summary is not None
                and summary.business_id == parameters["business_id"]
                and summary.conversation_id == parameters["conversation_id"]
                and summary.generation_attempts == parameters["attempt"]
                and any(
                    r.conversation_summary_id == summary.id
                    and r.business_id == summary.business_id
                    and r.generation_attempt == parameters["attempt"]
                    and r.status != "released"
                    for r in self.rows.get(AIUsageReservation, ())
                )
            )
        elif "sou2ai_reconcile_ai_usage(" in sql:
            reservation = self.get(AIUsageReservation, parameters["reservation_id"])
            changed = reservation.status == "reserved"
            if changed:
                reservation.status = parameters["outcome"]
                total = (
                    parameters["input_tokens"]
                    if parameters["input_tokens"] is not None
                    else reservation.estimated_input_tokens
                ) + (
                    parameters["output_tokens"]
                    if parameters["output_tokens"] is not None
                    else reservation.max_output_tokens
                )
                self.charges[reservation.id] = (
                    total if parameters["outcome"] != "release" else 0
                )
            row = SimpleNamespace(reconciled=changed)
        elif "sou2ai_resize_owner_chat_usage(" in sql:
            reservation = self.get(AIUsageReservation, parameters["reservation_id"])
            assert reservation.status == "reserved"
            reservation.estimated_input_tokens = parameters["estimated_input"]
            reservation.max_output_tokens = parameters["max_output"]
            row = SimpleNamespace()
        else:
            raise AssertionError(f"Unimplemented offline SQL function: {sql}")
        return SimpleNamespace(
            one=lambda: row, one_or_none=lambda: row, scalar_one=lambda: row
        )

    def expire_all(self):
        pass

    def commit(self):
        super().commit()
        self.snapshot = {model: list(rows) for model, rows in self.rows.items()}
        self.snapshots = [
            (
                row,
                {
                    c.key: copy.deepcopy(getattr(row, c.key))
                    for c in row.__table__.columns
                },
            )
            for rows in self.rows.values()
            for row in rows
        ]
        self.committed_charges = self.charges.copy()

    def rollback(self):
        self.rows = {model: list(rows) for model, rows in self.snapshot.items()}
        for row, attributes in self.snapshots:
            for key, value in attributes.items():
                set_committed_value(row, key, copy.deepcopy(value))
        self.charges = self.committed_charges.copy()


@pytest.fixture
def integrated(monkeypatch):
    connect = socket.socket.connect

    def deny(sock, address):
        # Windows asyncio implements its self-pipe using this exact stdlib
        # socketpair call site. No HTTP/database caller receives this exception.
        if (
            sys._getframe(1).f_code
            is getattr(socket, "_fallback_socketpair", lambda: None).__code__
        ):
            return connect(sock, address)
        raise AssertionError("No external provider/database connection is permitted")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(
        Engine,
        "connect",
        lambda *_args, **_kwargs: pytest.fail(
            "No platform database connection is permitted"
        ),
    )
    monkeypatch.setattr(owner_chat, "utc_now", lambda: NOW)
    monkeypatch.setattr(conversations, "utc_now", lambda: NOW)
    monkeypatch.setattr(ai_usage, "utc_now", lambda: NOW)
    session = TransactionalPlatform()
    user = User(id=uuid.uuid4(), status=AccountStatus.ACTIVE)
    session.add(user)
    fixtures = [mapped_catalogue(250, "text"), mapped_catalogue(251, "composite")]
    businesses, sources, profiles = [], [], {}
    for index, fixture in enumerate(fixtures):
        business = Business(
            id=uuid.uuid4(),
            owner_user_id=user.id,
            name=f"Business-{uuid.uuid4().hex[:12]}",
            description="Independent generated integration business profile.",
            category=BusinessCategory.OTHER,
            custom_category="General merchandise",
            status=owner_chat.BusinessStatus.ACTIVE,
            timezone="Asia/Beirut",
            default_language=DefaultLanguage.EN,
            governorate="Beirut",
            district="Beirut",
            city="Beirut",
            address_line="Fixture address",
            opening_days=[
                BusinessOpeningDay(day_of_week=day, is_open=False, shifts=[])
                for day in range(7)
            ],
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
        key = f"isolated_{index}"
        source = OperationalDataSourceConfig(
            id=uuid.uuid4(),
            business_id=business.id,
            status=OperationalDataSourceStatus.ACTIVE,
            mapping_profile_key="discovered_products",
            mapping_profile_version=1,
            adapter_type="postgresql_readonly",
            connection_profile_key=key,
            updated_at=NOW,
        )
        session.add(source)
        session.add(
            SourceMappingRevision(
                id=uuid.uuid4(),
                source_id=source.id,
                business_id=business.id,
                version=1,
                status="approved",
                discovery=fixture.discovery.model_dump(mode="json"),
                schema_fingerprint=fixture.discovery.schema_fingerprint,
                approved_mapping=fixture.mapping.model_dump(mode="json"),
                approved_by=user.id,
                approved_at=NOW,
            )
        )
        profiles[key] = ConnectionProfile(
            key,
            "Fixture",
            "Controlled",
            "postgresql_readonly",
            "discovered_products",
            1,
            (business.id,),
        )
        businesses.append(business)
        sources.append(source)
    registry = SimpleNamespace(
        get_profile=lambda key: profiles.get(key),
        get_mapping=lambda *_: None,
        connector=lambda key, business_id: fixtures[int(key.rsplit("_", 1)[1])].source,
    )
    settings = Settings(
        _env_file=None,
        environment="testing",
        tool_call_audit_hmac_secret="offline-audit-only",
        conversation_summary_provider="mock",
        customer_chat_provider="mock",
    )
    session.commit()
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/api/v1")

    @app.middleware("http")
    async def request_id(request, call_next):
        request.state.request_id = str(uuid.uuid4())
        return await call_next(request)

    app.dependency_overrides[get_db_session] = lambda: session
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_connection_profile_registry] = lambda: registry
    app.dependency_overrides[run_security_record_cleanup] = lambda: None
    token, _ = create_access_token(user.id, settings)
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        yield SimpleNamespace(
            client=client,
            app=app,
            session=session,
            user=user,
            businesses=businesses,
            sources=sources,
            fixtures=fixtures,
            registry=registry,
            settings=settings,
        )
    for fixture in fixtures:
        fixture.source.close()


def path(system, index=0):
    return f"/api/v1/businesses/{system.businesses[index].id}"


def create_chat(system, index=0):
    response = system.client.post(path(system, index) + "/conversations")
    assert response.status_code == 201, response.text
    return path(system, index) + f"/conversations/{response.json()['id']}/messages"


def send(system, url, content, key=None):
    return system.client.post(
        url, json=dict(content=content, idempotency_key=key or str(uuid.uuid4()))
    )


def test_authenticated_multi_business_search_selection_persistence_and_replay(
    integrated,
):
    s = integrated
    first, second = create_chat(s), create_chat(s, 1)
    provider, calls = offline_provider(
        product_plan(s.fixtures[0].family), product_plan(s.fixtures[1].family)
    )
    s.app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    key = str(uuid.uuid4())
    first_result = send(s, first, f"List all {s.fixtures[0].family} products", key)
    assert first_result.status_code == 200, first_result.text
    reply = first_result.json()["assistant_message"]["content"]
    assert s.fixtures[0].family in reply and "ID: 0001" in reply
    replay = send(s, first, f"List all {s.fixtures[0].family} products", key)
    assert replay.status_code == 200 and replay.json()["replayed"]
    assert (
        replay.json()["assistant_message"]["id"]
        == first_result.json()["assistant_message"]["id"]
    )
    conflict = send(s, first, "Different content", key)
    assert conflict.status_code == 409
    second_result = send(s, second, f"اعرض منتجات {s.fixtures[1].family}")
    assert second_result.status_code == 200, second_result.text
    assert (
        s.fixtures[0].family not in second_result.json()["assistant_message"]["content"]
    )
    selection = send(s, first, "0002")
    assert selection.status_code == 200, selection.text
    assert len(calls) == 2  # Selection is deterministic and re-reads the scoped key.
    assert len(s.session.charges) == 2 and set(s.session.charges.values()) == {180}
    assert len(s.session.rows[ToolCallLog]) == 3
    assert all(
        log.args_hash and s.fixtures[0].family not in log.args_hash
        for log in s.session.rows[ToolCallLog]
    )
    history = s.client.get(first)
    assert history.status_code == 200 and len(history.json()["items"]) == 4
    assert all(
        s.fixtures[1].family not in m["content"] for m in history.json()["items"]
    )
    wrong = s.client.get(path(s, 1) + first[first.index("/conversations/") :])
    assert wrong.status_code == 404


@pytest.mark.parametrize("credential", [None, "invalid", "other_user", "disabled"])
def test_http_authentication_and_tenant_denial_precede_source_queries(
    integrated, credential
):
    s = integrated
    provider, calls = offline_provider()
    s.app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    if credential == "other_user":
        s.session.add(User(id=uuid.uuid4(), status=AccountStatus.ACTIVE))
        token, _ = create_access_token(s.session.rows[User][-1].id, s.settings)
        s.client.headers["Authorization"] = f"Bearer {token}"
    elif credential == "disabled":
        s.user.status = AccountStatus.DISABLED
    elif credential is None:
        del s.client.headers["Authorization"]
    else:
        s.client.headers["Authorization"] = "Bearer invalid"
    response = s.client.post(path(s) + "/conversations")
    assert (
        response.status_code
        == {None: 401, "invalid": 401, "other_user": 404, "disabled": 403}[credential]
    )
    assert not calls and not any(f.source.calls for f in s.fixtures)
    assert "request_id" in response.json()["error"]


def test_conversation_archive_rejects_new_turns_without_dispatch(integrated):
    s = integrated
    url = create_chat(s)
    provider, calls = offline_provider()
    s.app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    archived = s.client.post(url.removesuffix("/messages") + "/archive")
    assert archived.status_code == 200 and archived.json()["archived"]
    rejected = send(s, url, "Show arbitrary products")
    assert rejected.status_code == 409 and not calls and not s.session.charges


@pytest.mark.parametrize(
    "capabilities",
    [
        ("products",),
        ("products", "inventory"),
        ("inventory", "sales_summaries"),
        ("sales_summaries",),
    ],
)
def test_partial_operational_profiles_expose_only_authorized_tools(
    integrated, capabilities
):
    s = integrated
    source = s.sources[0]
    source.mapping_profile_key = FAKE_STORE_MAPPING.key
    mapping = replace(FAKE_STORE_MAPPING, required_capabilities=capabilities)
    profile = ConnectionProfile(
        source.connection_profile_key,
        "Fixture",
        "Controlled",
        source.adapter_type,
        mapping.key,
        1,
        (source.business_id,),
    )
    s.registry.get_profile = lambda _: profile
    s.registry.get_mapping = lambda *_: mapping
    fixture = legacy_source(901, NOW, "LBP", "Asia/Beirut")
    fixture.source.readonly()
    s.fixtures.append(fixture)
    s.registry.resolve = lambda _: fixture.adapter
    s.session.commit()
    executor = OperationalToolExecutor(s.session, s.registry, s.settings)
    definitions = executor.available_definitions(s.user, source.business_id)
    assert {d.capability for d in definitions} == set(capabilities)
    unsupported = (
        "sales_summary"
        if "sales_summaries" not in capabilities
        else "restocking_recommendations"
    )
    with pytest.raises(ToolExecutionError, match="capability_unavailable"):
        executor.execute(
            user=s.user,
            business_id=source.business_id,
            tool_name=unsupported,
            arguments={"start_date": "2026-01-01", "end_date": "2026-01-02"}
            if unsupported == "sales_summary"
            else {},
        )
    assert not any(
        "stock_levels" in sql or "filtered_sales" in sql
        for sql, _ in fixture.source.calls
    )


def test_explicit_operational_profile_allowlist_cannot_be_rebound(integrated):
    s = integrated
    source = s.sources[0]
    source.mapping_profile_key = FAKE_STORE_MAPPING.key
    profile = ConnectionProfile(
        source.connection_profile_key,
        "Fixture",
        "Controlled",
        source.adapter_type,
        FAKE_STORE_MAPPING.key,
        1,
        (s.businesses[1].id,),
    )
    s.registry.get_profile = lambda _: profile
    s.registry.get_mapping = lambda *_: FAKE_STORE_MAPPING
    executor = OperationalToolExecutor(s.session, s.registry, s.settings)
    assert not executor.available_definitions(s.user, source.business_id)
    with pytest.raises(ToolExecutionError, match="integration_unavailable"):
        executor.execute(
            user=s.user,
            business_id=source.business_id,
            tool_name="product_search",
            arguments={"query": "anything"},
        )
    assert not s.fixtures[0].source.calls


@pytest.mark.parametrize(
    "capabilities",
    [
        (),
        ("unknown",),
        ("products", "products"),
        ("restocking_recommendations",),
        ("best_sellers",),
    ],
)
def test_invalid_capabilities_fail_closed(capabilities):
    with pytest.raises(MappingProfileError):
        replace(
            FAKE_STORE_MAPPING, required_capabilities=capabilities
        ).validate_definition()


def test_lost_provider_response_fails_once_and_replay_preserves_uncertain_usage(
    integrated,
):
    s = integrated
    url = create_chat(s)
    dispatched = []

    def timeout(request):
        dispatched.append(request)
        raise httpx.ReadTimeout("private prompt secret SQL parameters", request=request)

    provider = GeminiOwnerChatProvider(
        api_key="offline",
        model="offline",
        timeout_seconds=1,
        transport=httpx.MockTransport(timeout),
    )
    s.app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    key = str(uuid.uuid4())
    failed = send(s, url, f"List all {s.fixtures[0].family} products", key)
    assert failed.status_code == 503, failed.text
    assert "private" not in failed.text
    repeated = send(s, url, f"List all {s.fixtures[0].family} products", key)
    assert repeated.status_code == 409
    assert len(dispatched) == 1 and len(s.session.charges) == 1
    assert s.session.rows[AIUsageReservation][0].status == "uncertain"
    assert next(iter(s.session.charges.values())) > 0


def configure_legacy(system, index, capabilities, currency):
    source = system.sources[index]
    source.mapping_profile_key = FAKE_STORE_MAPPING.key
    mapping = replace(
        FAKE_STORE_MAPPING,
        required_capabilities=capabilities,
        currency=currency,
        source_location_types={"branch": ("SHOP",), "warehouse": ("DEPOT",)},
    )
    profile = ConnectionProfile(
        source.connection_profile_key,
        "Fixture",
        "Controlled",
        source.adapter_type,
        mapping.key,
        1,
        (source.business_id,),
    )
    fixture = legacy_source(
        uuid.uuid4().hex[:12], NOW, currency, mapping.source_timezone
    )
    fixture.source.readonly()
    system.fixtures.append(fixture)
    system.registry.get_profile = lambda _: profile
    system.registry.get_mapping = lambda *_: mapping
    system.registry.resolve = lambda _: fixture.adapter
    system.session.commit()
    return fixture


@pytest.mark.parametrize("language,currency", [("en", "EUR"), ("ar", "JPY")])
def test_http_inventory_uses_source_quantities_and_shared_presentation(
    integrated, language, currency
):
    s = integrated
    fixture = configure_legacy(s, 0, ("products", "inventory"), currency)
    url = create_chat(s)
    provider, calls = offline_provider(
        dict(
            decision="tool",
            semantic_operation="inventory_list",
            tool_name="current_inventory",
            arguments={},
        )
    )
    s.app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    response = send(
        s, url, "اعرض المخزون الحالي" if language == "ar" else "Show current inventory"
    )
    assert response.status_code == 200, response.text
    reply = response.json()["assistant_message"]["content"]
    assert fixture.name in reply and "7.5" in reply and "4" in reply
    assert len(calls) == 1 and set(s.session.charges.values()) == {180}
    assert any("normalized_stock" in sql for sql, _ in fixture.source.calls)
    assert s.client.get(url).json()["items"][-1]["content"] == reply


@pytest.mark.parametrize("metric", ["revenue", "gross_profit"])
def test_http_sales_capability_uses_source_currency_and_rejects_missing_costs(
    integrated, metric
):
    s = integrated
    fixture = configure_legacy(s, 0, ("inventory", "sales_summaries"), "EUR")
    fixture.source.db.execute("PRAGMA query_only = OFF")
    fixture.source.db.execute(
        "INSERT INTO minimarket.receipts VALUES (1,?,?,1)",
        ("COMPLETED", "2026-08-01 12:00:00+00:00"),
    )
    fixture.source.db.execute(
        "INSERT INTO minimarket.receipt_lines VALUES (1,1,1,2.5,123.5)"
    )
    fixture.source.readonly()
    url = create_chat(s)
    provider, calls = offline_provider(
        dict(
            decision="tool",
            semantic_operation="sales_summary",
            tool_name="sales_summary",
            arguments=dict(
                metric=metric, start_date="2026-08-01", end_date="2026-08-02"
            ),
        )
    )
    s.app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    response = send(
        s,
        url,
        f"{'Revenue' if metric == 'revenue' else 'Gross profit'} "
        "for 2026-08-01 to 2026-08-02",
    )
    assert response.status_code == 200, response.text
    reply = response.json()["assistant_message"]["content"]
    if metric == "revenue":
        assert "123.5 EUR" in reply
        assert any("filtered_sales" in sql for sql, _ in fixture.source.calls)
    else:
        assert "123.5" not in reply
        assert not any("filtered_sales" in sql for sql, _ in fixture.source.calls)
        assert "cost" in reply.lower()
    assert len(calls) == 1 and set(s.session.charges.values()) == {180}


def test_http_unavailable_capability_does_not_dispatch_provider(integrated):
    s = integrated
    s.sources[0].status = OperationalDataSourceStatus.DISABLED
    s.session.commit()
    url = create_chat(s)
    provider, calls = offline_provider()
    s.app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    response = send(s, url, "Show current inventory")
    assert response.status_code == 200, response.text
    assert not calls and not s.session.charges and not s.fixtures[0].source.calls


def test_http_retry_keeps_one_reservation_and_finalizes_once(integrated, monkeypatch):
    s = integrated
    url = create_chat(s)
    dispatched = []
    plan = product_plan(s.fixtures[0].family)

    def response(request):
        dispatched.append(request)
        if len(dispatched) == 1:
            return httpx.Response(
                429,
                json={"error": {"status": "RESOURCE_EXHAUSTED"}},
                headers={"Retry-After": "1"},
            )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": json.dumps(plan)}]},
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 80,
                    "totalTokenCount": 180,
                },
            },
        )

    provider = GeminiOwnerChatProvider(
        api_key="offline",
        model="offline",
        timeout_seconds=1,
        transport=httpx.MockTransport(response),
    )
    s.app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    delays = []
    monkeypatch.setattr(owner_chat.time, "sleep", delays.append)
    key = str(uuid.uuid4())
    result = send(s, url, f"List all {s.fixtures[0].family} products", key)
    assert result.status_code == 200, result.text
    assert len(dispatched) == 2 and delays == [1]
    assert len(s.session.rows[AIUsageReservation]) == 1 and set(
        s.session.charges.values()
    ) == {180}
    assert send(s, url, f"List all {s.fixtures[0].family} products", key).json()[
        "replayed"
    ]
    assert len(dispatched) == 2


def wire_worker(system, monkeypatch, contract=None):
    monkeypatch.setattr(memory, "get_settings", lambda: system.settings)
    monkeypatch.setattr(memory, "get_session_factory", lambda: lambda: system.session)
    monkeypatch.setattr(memory, "utc_now", lambda: NOW)
    job = SimpleNamespace(
        id="isolated-summary",
        meta={"runtime_contract": contract or job_contract(system.settings, "summary")},
    )
    monkeypatch.setattr(worker_runtime, "get_current_job", lambda: job)


def test_http_history_to_worker_checkpoint_is_chronological_and_tenant_scoped(
    integrated, monkeypatch
):
    s = integrated
    urls = [create_chat(s), create_chat(s, 1)]
    plans = [
        product_plan(s.fixtures[index].family) for _ in range(9) for index in range(2)
    ]
    provider, calls = offline_provider(*plans)
    s.app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    for _ in range(9):
        for index in range(2):
            result = send(
                s, urls[index], f"List all {s.fixtures[index].family} products"
            )
            assert result.status_code == 200, result.text
    conversation_id = uuid.UUID(urls[0].split("/")[-2])
    original = [
        (message.id, message.content) for message in s.session.rows[OwnerChatMessage]
    ]
    wire_worker(s, monkeypatch)
    memory.process_conversation_summary(str(conversation_id))
    summary = s.session.rows[OwnerConversationSummary][0]
    assert summary.business_id == s.businesses[0].id and summary.summary_version == 1
    entries = json.loads(summary.content)["entries"]
    retained = [
        m
        for m in s.session.rows[OwnerChatMessage]
        if m.conversation_id == conversation_id
        and m.sequence_number <= summary.summarized_through_sequence_number
    ]
    retained.sort(key=lambda m: m.sequence_number)
    assert entries == [f"{m.role}: {m.content}" for m in retained]
    assert summary.summarized_through_sequence_number == retained[-1].sequence_number
    assert s.fixtures[1].family not in summary.content
    assert [
        (message.id, message.content) for message in s.session.rows[OwnerChatMessage]
    ] == original
    assert len(calls) == 18
    # Drain any remaining eligible whole batches, then retry the completed job.
    for _ in range(3):
        memory.process_conversation_summary(str(conversation_id))
    before = (
        summary.summary_version,
        summary.content,
        s.session.charges.copy(),
        len(s.session.function_calls),
    )
    memory.process_conversation_summary(str(conversation_id))
    assert (
        summary.summary_version,
        summary.content,
        s.session.charges,
        len(s.session.function_calls),
    ) == before
    assert (
        summary.summarized_through_sequence_number <= 6
    )  # Six completed recent turns remain outside memory.


@pytest.mark.parametrize("legacy", [True, False])
def test_worker_legacy_records_and_mismatched_code_are_preserved(
    integrated, monkeypatch, legacy
):
    s = integrated
    url = create_chat(s)
    conversation_id = uuid.UUID(url.split("/")[-2])
    row = summary_row(
        business_id=s.businesses[0].id,
        conversation_id=conversation_id,
        content="Historical opaque summary",
        summary_version=1,
        summarized_through_sequence_number=2,
        last_charged_through_sequence_number=2,
        provider_identifier="mock",
        model_identifier="deterministic-v1",
    )
    s.session.add(row)
    s.session.commit()
    contract = job_contract(s.settings, "summary")
    if not legacy:
        contract["source_version"] = "old-worker"
    wire_worker(s, monkeypatch, contract)
    if legacy:
        memory.process_conversation_summary(str(conversation_id))
        assert row.last_failure_code == "summary_legacy_review_required"
    else:
        before = len(s.session.statements)
        with pytest.raises(RuntimeError, match="worker_runtime_contract_mismatch"):
            memory.process_conversation_summary(str(conversation_id))
        assert len(s.session.statements) == before
    assert (
        row.content == "Historical opaque summary"
        and row.summarized_through_sequence_number == 2
    )
    assert not s.session.charges and not s.session.function_calls


def test_http_late_provider_result_cannot_finalize_an_expired_claim(integrated):
    s = integrated
    url = create_chat(s)
    provider, calls = offline_provider(product_plan(s.fixtures[0].family))
    generate = provider.generate

    def late(request):
        result = generate(request)
        s.session.now = NOW + timedelta(
            seconds=s.settings.owner_chat_generation_lease_seconds + 1
        )
        return result

    provider.generate = late
    s.app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    key = str(uuid.uuid4())
    result = send(s, url, f"List all {s.fixtures[0].family} products", key)
    assert result.status_code == 409, result.text
    messages = s.client.get(url).json()["items"]
    assert len(messages) == 1 and messages[0]["generation_state"] == "failed"
    replay = send(s, url, f"List all {s.fixtures[0].family} products", key)
    assert replay.status_code == 409 and len(calls) == 1
    assert len(s.session.charges) == 1 and set(s.session.charges.values()) == {180}


def test_http_mapping_revision_during_query_discards_results_and_preserves_audit(
    integrated, monkeypatch
):
    s = integrated
    url = create_chat(s)
    provider, calls = offline_provider(product_plan(s.fixtures[0].family))
    s.app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    source = s.fixtures[0].source
    execute = source.execute
    raced_queries = []

    def revised(statement, parameters=None):
        result = execute(statement, parameters)
        # Full-scope health validation also executes SQL before planning. Race
        # the actual bound product lookup, not those validation aggregates.
        if not parameters or "exact" not in parameters:
            return result
        raced_queries.append(statement)
        previous = s.session.rows[SourceMappingRevision][0]
        s.session.add(
            SourceMappingRevision(
                id=uuid.uuid4(),
                source_id=previous.source_id,
                business_id=previous.business_id,
                version=2,
                status="approved",
                discovery=previous.discovery,
                schema_fingerprint=previous.schema_fingerprint,
                approved_mapping=previous.approved_mapping,
                approved_by=s.user.id,
                approved_at=NOW,
            )
        )
        s.session.commit()
        return result

    monkeypatch.setattr(source, "execute", revised)
    result = send(s, url, f"List all {s.fixtures[0].family} products")
    assert result.status_code == 200, result.text
    assert s.fixtures[0].family not in result.json()["assistant_message"]["content"]
    assert len(calls) == 1 and len(raced_queries) == 1
    assert s.session.rows[ToolCallLog][-1].error_code == "integration_unavailable"
    assert len(s.client.get(url).json()["items"]) == 2
    assert set(s.session.charges.values()) == {180}
