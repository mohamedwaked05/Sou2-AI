"""Phase 3 regressions. Run with --confcutdir=tests/offline; no live services."""

import json
import logging
import socket
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier, RLock
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from app.agent.owner_chat_provider import (
    ConversationSummaryRequest,
    ConversationSummaryResult,
    DeterministicMockOwnerChatProvider,
    GeminiOwnerChatProvider,
    OllamaOwnerChatProvider,
    OwnerChatProviderInvalidResponse,
    OwnerChatProviderTimeout,
    OwnerChatProviderUnavailable,
    OwnerChatRequest,
    OwnerChatResult,
    ProviderMessage,
    TokenUsage,
)
from app.core import runtime
from app.core.config import Settings
from app.core.exceptions import ApplicationError
from app.core.logging import (
    PrivacySafeConsoleFormatter,
    ProductionJSONFormatter,
    SensitiveDataFilter,
    request_id_context,
)
from app.database.models import (
    AIUsageReservation,
    ChatGenerationState,
    ChatMessageRole,
    ConversationSummaryState,
    OwnerChatMessage,
    OwnerConversation,
    OwnerConversationSummary,
)
from app.services import ai_usage, owner_chat
from app.worker import conversation_summary as memory
from app.worker import customer_messages as customer
from app.worker.runtime import PrivacySafeWorker
from pydantic import ValidationError
from rq import Worker
from sqlalchemy import Engine
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BinaryExpression
from sqlalchemy.sql.functions import Function
from sqlalchemy.sql.selectable import ScalarSelect
from test_catalogue_chat import PROFILE
from test_owner_chat_state import StateSession

NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)
USAGE = TokenUsage(10, 5, 15, True)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Live network access is forbidden in offline tests.")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket, "create_connection", reject)
    monkeypatch.setattr(Engine, "connect", reject)


def settings(**overrides):
    return Settings(
        _env_file=None,
        environment="testing",
        conversation_summary_provider="mock",
        customer_chat_provider="mock",
        **overrides,
    )


def request(turn, size=90, previous=None):
    return ConversationSummaryRequest(
        previous,
        (
            ProviderMessage("owner", f"owner turn {turn}: " + "أ" * size),
            ProviderMessage("assistant", f"assistant turn {turn}: acknowledged"),
        ),
    )


def summary_row(**overrides):
    values = dict(
        id=uuid.uuid4(),
        business_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        content=None,
        summary_version=0,
        summarized_through_sequence_number=0,
        last_charged_through_sequence_number=0,
        generation_attempts=0,
        generation_state=ConversationSummaryState.IDLE,
        pending_through_sequence_number=None,
        generation_claim_token=None,
        generation_claim_expires_at=None,
        last_failure_code=None,
        provider_identifier=None,
        model_identifier=None,
    )
    values.update(overrides)
    return OwnerConversationSummary(**values)


def session_for(summary):
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    conversation = SimpleNamespace(
        id=summary.conversation_id, business_id=summary.business_id
    )

    def scalar(statement):
        sql = str(statement)
        if "clock_timestamp" in sql and "FROM" not in sql:
            return NOW
        if "FROM owner_conversations" in sql:
            return conversation
        if "FROM ai_usage_reservations" in sql:
            return session.dispatched_reservation
        return summary

    session.scalar.side_effect = scalar
    session.dispatched_reservation = None
    session.execute.return_value.scalar_one.side_effect = lambda: (
        session.dispatched_reservation is not None
    )
    session.get.return_value = summary
    return session


def wire_memory(monkeypatch, summary, count=8):
    session = session_for(summary)
    monkeypatch.setattr(memory, "get_session_factory", lambda: lambda: session)
    monkeypatch.setattr(memory, "get_settings", settings)
    monkeypatch.setattr(runtime, "get_current_job", lambda: None)
    monkeypatch.setattr(memory, "utc_now", lambda: NOW)
    turns = [
        (
            SimpleNamespace(
                role="owner", content=f"fact {i}", sequence_number=2 * i - 1
            ),
            SimpleNamespace(
                role="assistant", content=f"claim {i}", sequence_number=2 * i
            ),
        )
        for i in range(1, count + 1)
    ]
    monkeypatch.setattr(memory, "_eligible_turns", lambda *args: turns)
    return session, turns


def test_deterministic_summary_retains_new_entries_in_order_over_many_rollovers():
    provider = DeterministicMockOwnerChatProvider()
    previous = None
    for turn in range(1, 30):
        req = request(turn, previous=previous)
        result = provider.summarize(req)
        entries = json.loads(result.summary)
        assert len(result.summary) <= 2000 and result.usage.output_tokens <= 256
        assert entries["entries"][-2:] == [
            f"{m.role}: {m.content}" for m in req.messages
        ]
        assert result.retained_message_count == 2
        assert result.model_identifier == "deterministic-v2"
        assert provider.summarize(req) == result
        previous = result.summary
        assert len(entries["entries"]) % 2 == 0
        assert entries["entries"][0].startswith("owner: ")
    assert entries["older_entries_omitted"] is True
    assert "owner turn 1:" not in previous and "owner turn 29:" in previous


@pytest.mark.parametrize("previous", ["legacy text", "x" * 2000, '{"entries":[]}'])
def test_opaque_legacy_summary_is_not_truncated_or_reconstructed(previous):
    with pytest.raises(OwnerChatProviderInvalidResponse) as caught:
        DeterministicMockOwnerChatProvider().summarize(request(2, previous=previous))
    assert caught.value.usage_uncertain is False


def test_new_oversized_turn_fails_without_prefix_truncation():
    with pytest.raises(OwnerChatProviderInvalidResponse):
        DeterministicMockOwnerChatProvider().summarize(request(1, size=3000))


def test_claim_selects_only_whole_new_turns_that_fit(monkeypatch):
    row = summary_row()
    session, turns = wire_memory(monkeypatch, row, count=16)
    for i, turn in enumerate(turns):
        turn[0].content = f"new fact {i}: " + "x" * 300
    claimed = memory._claim_summary(row.conversation_id, settings())
    assert claimed is not None and claimed[2:] == (0, 4)
    assert row.summarized_through_sequence_number == 0
    assert row.pending_through_sequence_number == 4
    assert session.commit.call_count == 2


def test_unretainable_first_turn_does_not_claim_or_advance(monkeypatch):
    row = summary_row()
    _, turns = wire_memory(monkeypatch, row)
    turns[0][0].content = "x" * 3000
    assert memory._claim_summary(row.conversation_id, settings()) is None
    assert row.summary_version == 0 and row.summarized_through_sequence_number == 0
    assert row.last_failure_code == "summary_retention_limit"


@pytest.mark.parametrize(
    "case",
    ["legacy", "charged", "expired_dispatch", "failed_dispatch", "blocked_retry"],
)
def test_summary_retry_never_skips_loss_or_dispatches_twice(monkeypatch, case):
    row = summary_row(
        content="retained historical content",
        summary_version=1,
        summarized_through_sequence_number=4,
        last_charged_through_sequence_number=4,
    )
    session, _ = wire_memory(monkeypatch, row, count=20)
    if case == "legacy":
        row.provider_identifier, row.model_identifier = "mock", "deterministic"
    elif case == "charged":
        row.last_charged_through_sequence_number = 8
    elif case == "expired_dispatch":
        row.generation_state = ConversationSummaryState.PROCESSING
        row.generation_claim_expires_at = NOW - timedelta(seconds=1)
        row.generation_claim_token = uuid.uuid4()
        row.pending_through_sequence_number = 8
        session.dispatched_reservation = uuid.uuid4()
    elif case == "failed_dispatch":
        row.generation_state = ConversationSummaryState.FAILED
        row.generation_attempts = 1
        session.dispatched_reservation = uuid.uuid4()
    else:
        row.last_failure_code = "summary_expired_dispatch_review_required"
    assert memory._claim_summary(row.conversation_id, settings()) is None
    assert memory._claim_summary(row.conversation_id, settings()) is None
    assert row.content == "retained historical content"
    assert row.summarized_through_sequence_number == 4 and row.summary_version == 1


@pytest.mark.parametrize(
    "case", ["oversized", "noop", "prefix", "coverage", "late", "reconcile", "success"]
)
def test_worker_advances_only_retained_reconciled_unexpired_summaries(
    monkeypatch, case
):
    token = uuid.uuid4()
    row = summary_row(
        content="previous valid summary",
        summary_version=1,
        generation_state=ConversationSummaryState.PROCESSING,
        generation_claim_token=token,
        generation_claim_expires_at=NOW + timedelta(minutes=2),
        pending_through_sequence_number=4,
    )
    session, _ = wire_memory(monkeypatch, row)
    monkeypatch.setattr(memory, "_claim_summary", lambda *args: (row.id, token, 0, 4))
    provider = Mock()
    provider.estimate_summary_input_tokens.return_value = 10
    content = {"oversized": "x" * 2001, "noop": row.content, "prefix": "previous"}.get(
        case, "new retained facts"
    )

    def summarize(req):
        if case == "late":
            row.generation_claim_expires_at = NOW - timedelta(seconds=1)
        return ConversationSummaryResult(
            content, USAGE, "test", "offline", 0 if case == "coverage" else 4
        )

    provider.summarize.side_effect = summarize
    monkeypatch.setattr(memory, "create_owner_chat_provider", lambda _: provider)
    monkeypatch.setattr(
        memory,
        "reserve_conversation_summary_usage",
        lambda *args, **kwargs: SimpleNamespace(
            id=uuid.uuid4(), reset_at=NOW + timedelta(hours=1)
        ),
    )
    reconcile = Mock(return_value=True)
    if case == "reconcile":
        reconcile.side_effect = RuntimeError("ai_usage_reconciliation_not_applied")
    monkeypatch.setattr(memory, "reconcile_ai_usage", reconcile)
    failed = Mock()
    monkeypatch.setattr(memory, "_fail_summary", failed)
    memory.process_conversation_summary(str(row.conversation_id))
    if case == "success":
        assert (
            row.content == "new retained facts"
            and row.summarized_through_sequence_number == 4
        )
        assert row.summary_version == 2 and reconcile.call_args.kwargs["require_new"]
        failed.assert_not_called()
    else:
        assert (
            row.content == "previous valid summary"
            and row.summarized_through_sequence_number == 0
        )
        assert row.summary_version == 1
        failed.assert_called_once()


def test_late_summary_usage_is_reconciled_without_touching_replaced_claim(monkeypatch):
    session = session_for(summary_row())
    session.scalar.side_effect = None
    session.scalar.return_value = None
    monkeypatch.setattr(memory, "get_session_factory", lambda: lambda: session)
    reconcile = Mock(return_value=False)
    monkeypatch.setattr(memory, "reconcile_ai_usage", reconcile)
    memory._fail_summary(
        uuid.uuid4(),
        uuid.uuid4(),
        "expired",
        SimpleNamespace(id=uuid.uuid4()),
        OwnerChatProviderUnavailable(usage=USAGE),
    )
    assert reconcile.call_args.kwargs["outcome"] == "reported_failure"
    session.commit.assert_called_once()


@pytest.mark.parametrize("provider", ["mock", "gemini"])
def test_worker_requires_explicit_provider_even_in_development(provider):
    configured = Settings(
        _env_file=None,
        environment="development",
        owner_chat_provider=provider,
        gemini_api_key="offline-placeholder",
    )
    with pytest.raises(RuntimeError, match="worker_provider_not_configured"):
        runtime.job_contract(configured, "summary")
    with pytest.raises(RuntimeError, match="worker_provider_not_configured"):
        runtime.job_contract(configured, "customer")


@pytest.mark.parametrize("environment", ["production", "staging", "typo"])
def test_mock_cannot_be_enabled_outside_local_environments(environment):
    with pytest.raises(ValidationError, match="Mock providers require"):
        Settings(
            _env_file=None,
            environment=environment,
            allowed_cors_origins=["https://sou2ai.example"],
        )


@pytest.mark.parametrize(
    "field", ["conversation_summary_provider", "customer_chat_provider"]
)
def test_worker_mock_is_rejected_even_when_other_providers_are_explicit_and_real(field):
    configured = dict(
        _env_file=None,
        environment="staging",
        owner_chat_provider="ollama",
        source_mapping_provider="gemini",
        gemini_api_key="offline-placeholder",
        conversation_summary_provider="ollama",
        customer_chat_provider="ollama",
    )
    configured[field] = "mock"
    with pytest.raises(ValidationError, match="Mock providers require"):
        Settings(**configured)


@pytest.mark.parametrize(
    "field", ["conversation_summary_provider", "customer_chat_provider"]
)
def test_worker_provider_validates_keys_and_deadlines(field):
    with pytest.raises(ValidationError, match="GEMINI_API_KEY"):
        Settings(_env_file=None, gemini_api_key=None, **{field: "gemini"})
    with pytest.raises(ValidationError, match="lease must exceed"):
        Settings(
            _env_file=None,
            gemini_api_key="offline-placeholder",
            gemini_request_timeout_seconds=160,
            **{field: "gemini"},
        )


@pytest.mark.parametrize(
    "mismatch",
    [
        "source_version",
        "contract_version",
        "provider",
        "model",
        "responsibility",
        "environment",
        "generation_lease_seconds",
        "provider_timeout_seconds",
        "legacy",
    ],
)
def test_worker_rejects_incompatible_api_job_before_claiming_work(
    monkeypatch, mismatch
):
    configured = settings()
    contract = runtime.job_contract(configured, "summary")
    if mismatch == "legacy":
        contract = None
    else:
        contract[mismatch] = "mismatch"
    job = SimpleNamespace(id=str(uuid.uuid4()), meta={"runtime_contract": contract})
    monkeypatch.setattr(runtime, "get_current_job", lambda: job)
    monkeypatch.setattr(memory, "get_settings", lambda: configured)
    claim = Mock()
    monkeypatch.setattr(memory, "_claim_summary", claim)
    with pytest.raises(RuntimeError, match="worker_runtime_contract_mismatch"):
        memory.process_conversation_summary(str(uuid.uuid4()))
    claim.assert_not_called()
    assert request_id_context.get() == job.id


def test_exact_job_contract_matches_without_network_and_does_not_contain_secrets(
    monkeypatch,
):
    configured = settings(owner_chat_provider="gemini", gemini_api_key="secret-fixture")
    contract = runtime.job_contract(configured, "summary")
    assert contract["provider"] == "mock" and len(contract["source_version"]) == 64
    assert "secret-fixture" not in json.dumps(contract)
    monkeypatch.setattr(
        runtime,
        "get_current_job",
        lambda: SimpleNamespace(id="offline-job", meta={"runtime_contract": contract}),
    )
    runtime.validate_worker_job(configured, "summary")


def test_success_reconciliation_rejects_missing_active_guard_result():
    session = Mock()
    session.execute.return_value.one_or_none.return_value = None
    session.scalar.return_value = NOW
    with pytest.raises(RuntimeError, match="ai_usage_reservation_not_active"):
        ai_usage.reconcile_ai_usage(
            session, uuid.uuid4(), usage=USAGE, outcome="completed", require_new=True
        )
    assert session.execute.call_count == 1
    assert "sou2ai_lock_active_ai_usage_reservation" in str(
        session.execute.call_args.args[0]
    )
    session.commit.assert_not_called()


@pytest.mark.parametrize("strict", [True, False])
def test_reconciliation_false_is_observable_and_success_cannot_commit_it(
    caplog, strict
):
    session = Mock()
    session.execute.return_value.one_or_none.return_value = SimpleNamespace(
        lease_expires_at=NOW + timedelta(minutes=1),
        window_end=NOW + timedelta(hours=1),
    )
    session.scalar.return_value = NOW
    session.execute.return_value.one.return_value = SimpleNamespace(reconciled=False)
    if strict:
        with pytest.raises(RuntimeError, match="ai_usage_reconciliation_not_applied"):
            ai_usage.reconcile_ai_usage(
                session,
                uuid.uuid4(),
                usage=USAGE,
                outcome="completed",
                require_new=True,
            )
        session.commit.assert_not_called()
    else:
        assert (
            ai_usage.reconcile_ai_usage(
                session, uuid.uuid4(), usage=None, outcome="uncertain"
            )
            is False
        )
        session.commit.assert_called_once()
    assert "already_finalized" in caplog.text


@pytest.mark.parametrize("row_exists", [True, False])
def test_owner_late_claim_cannot_persist_a_response(row_exists):
    session = Mock()
    session.scalar.side_effect = (
        [SimpleNamespace(generation_claim_expires_at=NOW), NOW]
        if row_exists
        else [None]
    )
    with pytest.raises(ApplicationError) as caught:
        owner_chat._persist_result(
            session,
            uuid.uuid4(),
            owner_chat._Claim(uuid.uuid4(), uuid.uuid4()),
            OwnerChatResult(reply="late result"),
            None,
            None,
            (),
        )
    assert caught.value.error_code == "owner_turn_failed"
    session.add.assert_not_called()
    session.commit.assert_not_called()
    session.rollback.assert_called_once()


@pytest.mark.parametrize("status", ["PROCESSING", "SENDING"])
def test_customer_expired_claim_cannot_finalize_even_if_still_marked_processing(status):
    session = Mock()
    session.scalar.side_effect = [SimpleNamespace(status=status), NOW]
    with pytest.raises(customer.CustomerClaimLost):
        customer._locked_customer_claim(
            session, uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), status, NOW
        )
    session.commit.assert_not_called()


@pytest.mark.parametrize(
    "case",
    ["success", "late", "rejected_reconciliation", "unexpected_provider_failure"],
)
def test_customer_generation_and_replay_dispatch_once_and_preserve_accounting(
    monkeypatch, case
):
    configured = settings()
    clock = SimpleNamespace(now=NOW)
    message = SimpleNamespace(
        id=uuid.uuid4(),
        business_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        status=customer.CustomerMessageStatus.RECEIVED,
        content="Hello there",
        claim_expires_at=None,
    )
    conversation = SimpleNamespace(
        id=message.conversation_id,
        business_id=message.business_id,
        connection_id=uuid.uuid4(),
        state=customer.CustomerConversationState.AI_ACTIVE,
    )
    connection = SimpleNamespace(
        business_id=message.business_id,
        status=customer.MessagingConnectionStatus.ACTIVE,
        auto_reply_enabled=True,
    )
    business = SimpleNamespace(
        id=message.business_id, status=customer.BusinessStatus.ACTIVE
    )
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)

    def scalar(statement):
        if "inbound_webhook_deliveries" in str(statement):
            return None
        return clock.now if "clock_timestamp" in str(statement) else message

    session.scalar.side_effect = scalar
    session.get.side_effect = lambda model, _: {
        customer.CustomerConversation: conversation,
        customer.MessagingChannelConnection: connection,
        customer.Business: business,
    }[model]
    committed_states = []
    session.commit.side_effect = lambda: committed_states.append(message.status)
    monkeypatch.setattr(customer, "get_session_factory", lambda: lambda: session)
    monkeypatch.setattr(
        customer, "recover_expired_customer_message_claims", lambda *args, **kwargs: 0
    )
    monkeypatch.setattr(customer, "utc_now", lambda: NOW)
    monkeypatch.setattr(runtime, "get_current_job", lambda: None)
    monkeypatch.setattr(
        customer,
        "_provider_request",
        lambda *args: OwnerChatRequest(
            profile=PROFILE,
            knowledge=(),
            messages=(),
            requested_at=NOW,
            mode="customer",
        ),
    )
    monkeypatch.setattr(customer, "_admit_rate", lambda *args: True)
    monkeypatch.setattr(
        customer,
        "reserve_customer_message_usage",
        lambda *args, **kwargs: SimpleNamespace(
            id=uuid.uuid4(), reset_at=NOW + timedelta(hours=1)
        ),
    )
    monkeypatch.setattr(
        customer, "_validate_customer_result", lambda *args: "A safe reply"
    )
    provider = Mock()
    provider.estimate_input_tokens.return_value = 10

    def generate(_):
        if case == "late":
            clock.now += timedelta(minutes=3)
        if case == "unexpected_provider_failure":
            raise RuntimeError("private provider source record")
        return OwnerChatResult(reply="A safe reply", usage=USAGE)

    provider.generate.side_effect = generate
    reconcile = Mock(return_value=True)

    def reconcile_result(*args, **kwargs):
        if case == "rejected_reconciliation" and kwargs.get("require_new"):
            raise RuntimeError("ai_usage_reconciliation_not_applied")
        return True

    reconcile.side_effect = reconcile_result
    monkeypatch.setattr(customer, "reconcile_ai_usage", reconcile)
    enqueue = Mock()
    monkeypatch.setattr(customer, "enqueue_outbound_message", enqueue)
    customer.process_inbound_message(
        str(message.id), provider=provider, settings_override=configured
    )
    calls = reconcile.call_count
    customer.process_inbound_message(
        str(message.id), provider=provider, settings_override=configured
    )
    assert provider.generate.call_count == 1 and reconcile.call_count == calls
    if case == "success":
        assert message.status == customer.CustomerMessageStatus.COMPLETED
        assert reconcile.call_args.kwargs["require_new"] is True
        enqueue.assert_called_once()
    else:
        assert message.status == customer.CustomerMessageStatus.FAILED
        assert customer.CustomerMessageStatus.COMPLETED not in committed_states
        enqueue.assert_not_called()
        assert reconcile.call_args.kwargs["outcome"] == (
            "uncertain" if case == "unexpected_provider_failure" else "reported_failure"
        )


def test_customer_failure_uses_recovery_lock_order(monkeypatch):
    events = []
    message = SimpleNamespace(status="PROCESSING")
    monkeypatch.setattr(
        customer,
        "_locked_customer_claim",
        lambda *args, **kwargs: events.append("message") or message,
    )
    monkeypatch.setattr(
        customer,
        "reconcile_ai_usage",
        lambda *args, **kwargs: events.append("reservation"),
    )
    session = Mock()
    session.commit.side_effect = lambda: events.append("commit")
    customer._fail_customer_generation(
        session,
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        NOW,
        uuid.uuid4(),
        None,
        "uncertain",
        None,
        None,
        "customer.generation_failed",
    )
    assert events == ["message", "reservation", "commit"]
    assert message.status == customer.CustomerMessageStatus.FAILED


@pytest.mark.parametrize(
    "formatter", [PrivacySafeConsoleFormatter(), ProductionJSONFormatter()]
)
def test_exception_logging_excludes_values_and_cached_tracebacks(formatter):
    try:
        raise ValueError("customer-secret record SELECT parameters credential")
    except ValueError:
        record = logging.LogRecord(
            "app.test",
            logging.ERROR,
            __file__,
            1,
            "failure classified",
            (),
            sys.exc_info(),
        )
    record.exc_text = "customer-secret cached traceback"
    record.stack_info = "customer-secret stack"
    SensitiveDataFilter().filter(record)
    rendered = formatter.format(record)
    assert "customer-secret" not in rendered and "ValueError" in rendered


def test_sql_logging_is_dropped_even_without_secret_shaped_parameters():
    record = logging.LogRecord(
        "sqlalchemy.engine.Engine",
        logging.ERROR,
        __file__,
        1,
        "SELECT '%s'",
        ("private customer data",),
        None,
    )
    assert SensitiveDataFilter().filter(record) is False


def test_rq_failure_record_and_log_never_contain_exception_payload(monkeypatch, caplog):
    worker = object.__new__(PrivacySafeWorker)
    worker.log = logging.getLogger("rq.worker.offline")
    saved = Mock()
    monkeypatch.setattr(Worker, "handle_job_failure", saved)
    job = SimpleNamespace(id="offline-job")
    try:
        raise ValueError("sensitive SQL customer bind values")
    except ValueError:
        worker.handle_exception(job, *sys.exc_info())
        worker.handle_job_failure(
            job, object(), exc_string="raw traceback sensitive SQL"
        )
    assert "sensitive" not in caplog.text and "ValueError" in caplog.text
    assert (
        saved.call_args.kwargs["exc_string"]
        == "ValueError: [REDACTED_INTERNAL_EXCEPTION]"
    )


@pytest.mark.parametrize("retained", [0, 1, 2])
def test_gemini_summary_retention_attestation_is_validated_offline(retained):
    def response(_):
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "summary": "Owner requested a weekly plan.",
                                            "retained_message_count": retained,
                                        }
                                    )
                                }
                            ]
                        },
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 5,
                    "totalTokenCount": 15,
                },
            },
        )

    provider = GeminiOwnerChatProvider(
        api_key="offline-placeholder",
        model="offline",
        timeout_seconds=1,
        transport=httpx.MockTransport(response),
    )
    if retained == 2:
        assert provider.summarize(request(1)).retained_message_count == 2
    else:
        with pytest.raises(OwnerChatProviderInvalidResponse):
            provider.summarize(request(1))


@pytest.mark.parametrize("provider_name", ["gemini", "ollama"])
@pytest.mark.parametrize("mode", ["summary", "conversation"])
@pytest.mark.parametrize("remaining", [0, 2])
def test_provider_deadline_rejects_expired_dispatch_and_late_response(
    monkeypatch, provider_name, mode, remaining
):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(owner_chat.time, "monotonic", lambda: clock.now)
    dispatched = []

    def response(http_request):
        dispatched.append(http_request)
        assert http_request.extensions["timeout"]["read"] == remaining
        clock.now = 3
        return httpx.Response(
            200,
            json={
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 5,
                    "totalTokenCount": 15,
                },
                "prompt_eval_count": 10,
                "eval_count": 5,
            },
        )

    kwargs = dict(
        model="offline", timeout_seconds=120, transport=httpx.MockTransport(response)
    )
    provider = (
        GeminiOwnerChatProvider(api_key="offline-placeholder", **kwargs)
        if provider_name == "gemini"
        else OllamaOwnerChatProvider(base_url="http://offline.test", **kwargs)
    )
    req = (
        request(1)
        if mode == "summary"
        else OwnerChatRequest(PROFILE, (), (), NOW, mode="conversation")
    )
    req = replace(req, provider_timeout_seconds=remaining)
    with pytest.raises(OwnerChatProviderTimeout) as caught:
        provider.summarize(req) if mode == "summary" else provider.generate(req)
    assert caught.value.reason == "turn_deadline"
    assert len(dispatched) == (1 if remaining else 0)
    assert caught.value.usage == (USAGE if remaining else None)
    assert caught.value.usage_uncertain is bool(remaining)


def test_accounting_wait_past_deadline_cannot_commit_success():
    session = Mock()
    session.execute.return_value.one_or_none.return_value = SimpleNamespace(
        lease_expires_at=NOW + timedelta(minutes=1),
        window_end=NOW + timedelta(hours=1),
    )
    session.scalar.side_effect = [NOW, NOW + timedelta(seconds=5)]
    session.execute.return_value.one.return_value = SimpleNamespace(reconciled=True)
    with pytest.raises(RuntimeError, match="ai_usage_finalization_deadline_expired"):
        ai_usage.reconcile_ai_usage(
            session,
            uuid.uuid4(),
            usage=USAGE,
            outcome="completed",
            require_new=True,
            finalization_deadline=NOW + timedelta(seconds=4),
        )
    assert session.execute.call_count == 2
    session.commit.assert_not_called()


def test_completed_summary_worker_replay_does_not_reserve_or_summarize_again(
    monkeypatch,
):
    row = summary_row()
    _, turns = wire_memory(monkeypatch, row)
    monkeypatch.setattr(
        memory,
        "_eligible_turns",
        lambda session, identifier, after: [
            turn for turn in turns if turn[0].sequence_number > after
        ],
    )
    provider = Mock(wraps=DeterministicMockOwnerChatProvider())
    monkeypatch.setattr(memory, "create_owner_chat_provider", lambda _: provider)
    reserve = Mock(
        return_value=SimpleNamespace(id=uuid.uuid4(), reset_at=NOW + timedelta(hours=1))
    )
    monkeypatch.setattr(memory, "reserve_conversation_summary_usage", reserve)
    reconcile = Mock(return_value=True)
    monkeypatch.setattr(memory, "reconcile_ai_usage", reconcile)
    memory.process_conversation_summary(str(row.conversation_id))
    saved = row.content
    memory.process_conversation_summary(str(row.conversation_id))
    assert row.summary_version == 1 and row.summarized_through_sequence_number == 4
    assert row.content == saved
    provider.summarize.assert_called_once()
    reserve.assert_called_once()
    reconcile.assert_called_once()


def test_memory_context_selects_only_its_conversation_and_keeps_original_records():
    session = StateSession()
    own, other = uuid.uuid4(), uuid.uuid4()
    for identifier, content in ((own, "Own summary"), (other, "Other private summary")):
        session.add(
            summary_row(
                conversation_id=identifier,
                content=content,
                summary_version=1,
                summarized_through_sequence_number=4,
            )
        )
        session.add(
            OwnerChatMessage(
                id=uuid.uuid4(),
                conversation_id=identifier,
                sequence_number=5,
                role=ChatMessageRole.OWNER,
                content=f"{content} recent",
                generation_state=ChatGenerationState.COMPLETED,
            )
        )
    current = OwnerChatMessage(
        id=uuid.uuid4(),
        conversation_id=own,
        sequence_number=7,
        role=ChatMessageRole.OWNER,
        content="Current question",
    )
    summary, messages = owner_chat._provider_context(session, current)
    assert summary == "Own summary"
    assert [message.content for message in messages] == [
        "Own summary recent",
        "Current question",
    ]
    assert len(session.rows[OwnerChatMessage]) == 2
    assert session.commits == 0


def test_exception_arguments_and_numeric_diagnostics_are_safe():
    record = logging.LogRecord(
        "app.test",
        logging.WARNING,
        __file__,
        1,
        "retry delay=%.3f error=%s",
        (0.5, ValueError("private customer source")),
        None,
    )
    record.request_id = str(uuid.uuid4())
    SensitiveDataFilter().filter(record)
    rendered = PrivacySafeConsoleFormatter().format(record)
    assert "delay=0.500" in rendered and "ValueError" in rendered
    assert "private" not in rendered
    assert (
        json.loads(ProductionJSONFormatter().format(record))["request_id"]
        == record.request_id
    )


@pytest.mark.parametrize(
    "reason", ["invalid_result_type", "invalid_reply", "invalid_citations"]
)
def test_service_validation_preserves_safe_reasons(reason):
    result = {
        "invalid_result_type": object(),
        "invalid_reply": OwnerChatResult(reply=""),
        "invalid_citations": OwnerChatResult(
            reply="Safe", cited_source_ids=("private-source",)
        ),
    }[reason]
    req = OwnerChatRequest(PROFILE, (), (), NOW)
    with pytest.raises(OwnerChatProviderInvalidResponse) as caught:
        owner_chat._validate_result(result, req)
    assert caught.value.reason == reason and "private" not in str(caught.value)


def test_settings_validation_traceback_does_not_echo_configuration_inputs():
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, gemini_request_timeout_seconds="private-config-value")
    assert "private-config-value" not in str(caught.value)
    assert "gemini_request_timeout_seconds" in str(caught.value)


def test_history_response_exposes_the_authorized_conversation_identity(monkeypatch):
    ledger = concurrent_ledger()
    session = StateSession()
    message = OwnerChatMessage(
        id=uuid.uuid4(),
        conversation_id=ledger.conversation.id,
        sequence_number=2,
        role=ChatMessageRole.ASSISTANT,
        content="Retained history",
        generation_state=None,
        created_at=NOW,
        citations=[],
    )
    session.add(message)
    monkeypatch.setattr(owner_chat, "load_full_access_business", lambda *args: None)
    monkeypatch.setattr(
        owner_chat,
        "get_default_conversation",
        lambda *args, **kwargs: ledger.conversation,
    )
    result = owner_chat.get_conversation_history(
        session,
        SimpleNamespace(id=uuid.uuid4()),
        ledger.conversation.business_id,
        None,
    )
    assert result.conversation_id == ledger.conversation.id
    assert [item.content for item in result.items] == [message.content]


class ConcurrentLedgerSession(StateSession):
    """Transactional row-lock/ledger double; never connects to any database.

    Production SELECT predicates and finalization functions execute unchanged.
    A shared lock simulates the contested owner and reservation rows only.
    This validates orchestration, not PostgreSQL's lock implementation.
    """

    def __init__(self, ledger):
        super().__init__()
        self.ledger = ledger
        self.rows = ledger.rows
        self.locked = False
        self.pending = []

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
            return self.ledger.now
        return super().evaluate(expression, env)

    def scalar(self, statement):
        if statement._for_update_arg is not None and not self.locked:
            assert self.ledger.lock.acquire(timeout=5), (
                "Contested offline lock timed out"
            )
            self.locked = True
            self.snapshot = (
                self.ledger.owner.generation_state,
                self.ledger.owner.generation_claim_token,
                self.ledger.owner.generation_claim_expires_at,
                self.ledger.reservation.status,
                self.ledger.charged,
            )
        if "clock_timestamp" in str(statement) and "FROM" not in str(statement):
            return self.ledger.now
        return super().scalar(statement)

    def execute(self, statement, parameters):
        assert parameters["reservation_id"] == self.ledger.reservation.id
        assert self.locked
        if "sou2ai_lock_active_ai_usage_reservation" in str(statement):
            reservation = self.ledger.reservation
            deadline = parameters["finalization_deadline"]
            active = (
                reservation.status == "reserved"
                and reservation.lease_expires_at > self.ledger.now
                and reservation.window_end > self.ledger.now
                and (deadline is None or deadline > self.ledger.now)
            )
            row = (
                SimpleNamespace(
                    lease_expires_at=reservation.lease_expires_at,
                    window_end=reservation.window_end,
                )
                if active
                else None
            )
            return SimpleNamespace(one_or_none=lambda: row)
        assert "sou2ai_reconcile_ai_usage" in str(statement)
        changed = self.ledger.reservation.status == "reserved"
        if changed:
            self.ledger.reservation.status = parameters["outcome"]
            self.ledger.charged += (
                parameters["input_tokens"] + parameters["output_tokens"]
            )
        return SimpleNamespace(one=lambda: SimpleNamespace(reconciled=changed))

    def add(self, row):
        self.pending.append(row)

    def add_all(self, rows):
        self.pending.extend(rows)

    def commit(self):
        for row in self.pending:
            self.rows.setdefault(type(row), []).append(row)
        self.pending.clear()
        super().commit()
        self._unlock()

    def rollback(self):
        if self.locked:
            (
                self.ledger.owner.generation_state,
                self.ledger.owner.generation_claim_token,
                self.ledger.owner.generation_claim_expires_at,
                self.ledger.reservation.status,
                self.ledger.charged,
            ) = self.snapshot
        self.pending.clear()
        self._unlock()

    def _unlock(self):
        if self.locked:
            self.locked = False
            self.ledger.lock.release()


def concurrent_ledger():
    conversation = OwnerConversation(id=uuid.uuid4(), business_id=uuid.uuid4())
    owner = OwnerChatMessage(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        sequence_number=1,
        role=ChatMessageRole.OWNER,
        content="Offline request",
        generation_state=ChatGenerationState.PROCESSING,
        generation_claim_token=uuid.uuid4(),
        generation_claim_expires_at=NOW + timedelta(minutes=1),
    )
    reservation = AIUsageReservation(
        id=uuid.uuid4(),
        status="reserved",
        lease_expires_at=NOW + timedelta(minutes=1),
        window_end=NOW + timedelta(hours=1),
    )
    return SimpleNamespace(
        rows={
            OwnerConversation: [conversation],
            OwnerChatMessage: [owner],
            AIUsageReservation: [reservation],
        },
        conversation=conversation,
        owner=owner,
        reservation=reservation,
        lock=RLock(),
        now=NOW,
        charged=0,
    )


def test_concurrent_owner_finalizers_persist_one_response_and_one_charge(monkeypatch):
    ledger = concurrent_ledger()
    monkeypatch.setattr(owner_chat, "upsert_proposed_knowledge", lambda *args: None)
    monkeypatch.setattr(owner_chat, "utc_now", lambda: NOW)
    claim = owner_chat._Claim(ledger.owner.id, ledger.owner.generation_claim_token)
    barrier = Barrier(2)

    def finalize():
        session = ConcurrentLedgerSession(ledger)
        barrier.wait(timeout=5)
        try:
            owner_chat._persist_result(
                session,
                ledger.conversation.business_id,
                claim,
                OwnerChatResult(reply="Safe response"),
                SimpleNamespace(id=ledger.reservation.id),
                USAGE,
                (),
            )
            return "completed"
        except ApplicationError as error:
            return error.error_code
        finally:
            session.rollback()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: finalize(), range(2)))
    assert sorted(results) == ["completed", "owner_turn_failed"]
    assert ledger.charged == USAGE.total_tokens
    assert len(ledger.rows[OwnerChatMessage]) == 2
    assert ledger.owner.generation_state == ChatGenerationState.COMPLETED


@pytest.mark.parametrize("mismatch", ["business", "token", "deadline"])
def test_finalization_evaluates_tenant_claim_and_deadline_predicates(
    monkeypatch, mismatch
):
    ledger = concurrent_ledger()
    session = ConcurrentLedgerSession(ledger)
    claim = owner_chat._Claim(ledger.owner.id, ledger.owner.generation_claim_token)
    business_id = ledger.conversation.business_id
    if mismatch == "business":
        business_id = uuid.uuid4()
    elif mismatch == "token":
        claim = owner_chat._Claim(ledger.owner.id, uuid.uuid4())
    else:
        ledger.now += timedelta(minutes=2)
    with pytest.raises(ApplicationError, match="could not be completed"):
        owner_chat._persist_result(
            session,
            business_id,
            claim,
            OwnerChatResult(reply="Late"),
            SimpleNamespace(id=ledger.reservation.id),
            USAGE,
            (),
        )
    session.rollback()
    assert ledger.charged == 0 and len(ledger.rows[OwnerChatMessage]) == 1
