"""Real PostgreSQL ACL, locking, persistence and worker compatibility checks."""

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from alembic import command
from app.agent.owner_chat_provider import OwnerChatResult, TokenUsage
from app.core.config import Settings
from app.core.exceptions import ApplicationError
from app.database.models import (
    Business,
    CustomerConversation,
    CustomerMessage,
    MessagingChannelConnection,
    OwnerChatMessage,
    OwnerConversation,
    OwnerConversationSummary,
    User,
)
from app.services.ai_usage import (
    reconcile_ai_usage,
    reserve_conversation_summary_usage,
    reserve_owner_chat_usage,
    summary_attempt_has_usage,
)
from app.services.owner_chat import _Claim, _persist_result
from app.worker import conversation_summary as worker
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

LOCK = "public.sou2ai_lock_active_ai_usage_reservation(uuid,timestamptz)"
SUMMARY = "public.sou2ai_summary_attempt_has_usage(uuid,uuid,uuid,integer)"


def catalog_snapshot(engine):
    with engine.connect() as connection:
        functions = dict(
            connection.execute(
                text(
                    "SELECT p.oid::regprocedure::text, row_to_json(p)::text "
                    "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname='public'"
                )
            ).all()
        )
        tables = connection.execute(
            text(
                "SELECT relname, relowner, relacl::text FROM pg_class "
                "WHERE relnamespace='public'::regnamespace ORDER BY relname"
            )
        ).all()
        columns = connection.execute(
            text(
                "SELECT c.relname,a.attname,a.attacl::text FROM pg_attribute a "
                "JOIN pg_class c ON c.oid=a.attrelid "
                "WHERE c.relnamespace='public'::regnamespace "
                "ORDER BY c.relname,a.attnum"
            )
        ).all()
        return functions, tables, columns


def owner_claim(platform, tenant, *, lease=120):
    user, business_id, _ = tenant
    with platform["factory"]() as session:
        conversation = OwnerConversation(
            business_id=business_id, creator_user_id=user.id
        )
        session.add(conversation)
        session.flush()
        message = OwnerChatMessage(
            conversation_id=conversation.id,
            sequence_number=1,
            role="owner",
            content="Isolated reservation request",
            idempotency_key=uuid.uuid4().hex,
            generation_state="processing",
            generation_claim_token=uuid.uuid4(),
            generation_claim_expires_at=datetime.now(UTC) + timedelta(seconds=lease),
            generation_attempts=1,
        )
        session.add(message)
        session.commit()
        reservation = reserve_owner_chat_usage(
            session,
            business=session.get(Business, business_id),
            user=session.get(User, user.id),
            owner_message_id=message.id,
            generation_attempt=1,
            estimated_input_tokens=20,
            max_output_tokens=30,
            lease_seconds=120,
        )
        return (
            business_id,
            conversation.id,
            message.id,
            _Claim(message.id, message.generation_claim_token),
            reservation,
        )


def ledger(platform, reservation):
    with platform["admin"].connect() as connection:
        return connection.execute(
            text(
                "SELECT r.status,r.total_tokens,r.counts_authoritative,"
                "d.total_tokens_used,d.tokens_reserved "
                "FROM ai_usage_reservations r JOIN business_ai_usage_daily d "
                "ON d.business_id=r.business_id AND d.window_start=r.window_start "
                "WHERE r.id=:id"
            ),
            {"id": reservation.id},
        ).one()


def lock_wait(platform, pid):
    until = time.monotonic() + 4
    while time.monotonic() < until:
        with platform["admin"].connect() as connection:
            if connection.scalar(
                text(
                    "SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=:pid"
                ),
                {"pid": pid},
            ):
                return
        time.sleep(0.01)
    pytest.fail("Competing connection did not reach a PostgreSQL lock wait")


def test_additive_catalog_and_actual_roles(platform):
    before, after = platform["before"], platform["after"]
    assert before[1:] == after[1:]
    assert set(after[0]) - set(before[0]) == {
        "sou2ai_lock_active_ai_usage_reservation(uuid,timestamp with time zone)",
        "sou2ai_summary_attempt_has_usage(uuid,uuid,uuid,integer)",
    }
    for signature, value in before[0].items():
        assert after[0][signature] == value
    with platform["runtime"].connect() as connection:
        assert connection.scalar(text("SELECT current_user")) == "sou2ai_runtime_login"
        assert connection.scalar(
            text("SELECT pg_has_role(current_user,'sou2ai_runtime','member')")
        )
        assert not connection.scalar(
            text(
                "SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls "
                "FROM pg_roles WHERE rolname=current_user"
            )
        )
    with platform["admin"].connect() as connection:
        for signature in (LOCK, SUMMARY):
            row = connection.execute(
                text(
                    "SELECT prosecdef,provolatile,proconfig,pg_get_userbyid(proowner) "
                    "FROM pg_proc WHERE oid=CAST(:sig AS regprocedure)"
                ),
                {"sig": signature},
            ).one()
            assert (
                row.prosecdef
                and row.proconfig == ["search_path=pg_catalog"]
                and row.pg_get_userbyid == "sou2ai_migrator"
            )
            assert row.provolatile == ("v" if signature == LOCK else "s")
            for role, allowed in (
                ("sou2ai_runtime_login", True),
                ("sou2ai_lifecycle_operator_login", False),
                ("reservation_test_unprivileged", False),
            ):
                assert (
                    connection.scalar(
                        text("SELECT has_function_privilege(:role,:sig,'EXECUTE')"),
                        {"role": role, "sig": signature},
                    )
                    is allowed
                )


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM public.ai_usage_reservations",
        "SELECT lease_expires_at FROM public.ai_usage_reservations",
        "UPDATE public.ai_usage_reservations SET status='released'",
        "DELETE FROM public.business_ai_usage_daily",
        "CREATE TABLE public.forbidden(id int)",
        "SET ROLE sou2ai_migrator",
    ],
)
def test_runtime_least_privilege(platform, statement):
    with (
        platform["runtime"].connect() as connection,
        pytest.raises(DBAPIError) as error,
    ):
        connection.execute(text(statement))
    assert error.value.orig.sqlstate == "42501"


@pytest.mark.parametrize("engine", ["operator", "unprivileged"])
@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM public.sou2ai_lock_active_ai_usage_reservation(NULL,NULL)",
        "SELECT public.sou2ai_summary_attempt_has_usage(NULL,NULL,NULL,NULL)",
    ],
)
def test_other_roles_denied(platform, engine, query):
    with platform[engine].connect() as connection, pytest.raises(DBAPIError) as error:
        connection.execute(text(query))
    assert error.value.orig.sqlstate == "42501"


def test_safe_search_path_and_narrow_read(platform, tenant):
    _, _, _, _, reservation = owner_claim(platform, tenant)
    with platform["runtime"].begin() as connection:
        connection.execute(
            text(
                "CREATE TEMP TABLE ai_usage_reservations(id uuid,status text,"
                "lease_expires_at timestamptz,window_end timestamptz)"
            )
        )
        connection.execute(
            text("CREATE TEMP TABLE owner_conversation_summaries(id uuid)")
        )
        connection.execute(text("SET LOCAL search_path=pg_temp,public,pg_catalog"))
        result = connection.execute(
            text(
                "SELECT * FROM public.sou2ai_lock_active_ai_usage_reservation(:id,NULL)"
            ),
            {"id": reservation.id},
        )
        assert list(result.keys()) == ["lease_expires_at", "window_end"]
        assert result.one().lease_expires_at > datetime.now(UTC)
        assert (
            connection.scalar(
                text(
                    "SELECT public.sou2ai_summary_attempt_has_usage"
                    "(NULL,NULL,NULL,NULL)"
                )
            )
            is False
        )
        assert (
            connection.execute(
                text(
                    "SELECT * FROM public.sou2ai_lock_active_ai_usage_reservation"
                    "(:id,NULL)"
                ),
                {"id": uuid.uuid4()},
            ).one_or_none()
            is None
        )
        connection.execute(text("DROP TABLE pg_temp.ai_usage_reservations"))
        connection.execute(text("DROP TABLE pg_temp.owner_conversation_summaries"))
    assert ledger(platform, reservation).status == "reserved"


@pytest.mark.parametrize("expiry", ["lease", "window", "claim"])
def test_expired_guards_and_uncertain_accounting(platform, tenant, expiry):
    _, _, _, _, reservation = owner_claim(platform, tenant)
    deadline = None
    if expiry == "claim":
        deadline = datetime.now(UTC) - timedelta(seconds=1)
    else:
        with platform["admin"].begin() as connection:
            field = "lease_expires_at" if expiry == "lease" else "window_end"
            connection.execute(
                text(
                    f"UPDATE ai_usage_reservations SET {field}=clock_timestamp() "
                    "WHERE id=:id"
                ),
                {"id": reservation.id},
            )
    with platform["factory"]() as session:
        with pytest.raises(RuntimeError, match="ai_usage_reservation_not_active"):
            reconcile_ai_usage(
                session,
                reservation.id,
                usage=TokenUsage(2, 3, 5, True),
                outcome="completed",
                require_new=True,
                finalization_deadline=deadline,
            )
        session.rollback()
        assert ledger(platform, reservation).status == "reserved"
        assert reconcile_ai_usage(
            session, reservation.id, usage=None, outcome="uncertain"
        )
        assert not reconcile_ai_usage(
            session, reservation.id, usage=None, outcome="uncertain"
        )
    assert ledger(platform, reservation) == ("charged", 50, False, 50, 0)


def test_owner_atomic_finalization_replay_and_tenant_scope(platform, tenant):
    business, conversation, message, claim, reservation = owner_claim(platform, tenant)
    with platform["factory"]() as session:
        with pytest.raises(ApplicationError):
            _persist_result(
                session,
                uuid.uuid4(),
                claim,
                OwnerChatResult(reply="Isolated result"),
                reservation,
                TokenUsage(2, 3, 5, True),
                (),
            )
        session.rollback()
        _persist_result(
            session,
            business,
            claim,
            OwnerChatResult(reply="Isolated result"),
            reservation,
            TokenUsage(2, 3, 5, True),
            (),
        )
        with pytest.raises(ApplicationError):
            _persist_result(
                session,
                business,
                claim,
                OwnerChatResult(reply="Duplicate"),
                reservation,
                TokenUsage(2, 3, 5, True),
                (),
            )
        session.rollback()
        assert not reconcile_ai_usage(
            session,
            reservation.id,
            usage=TokenUsage(100, 100, 200, True),
            outcome="completed",
        )
    assert ledger(platform, reservation) == ("completed", 5, True, 5, 0)
    with platform["runtime"].connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM owner_chat_messages "
                    "WHERE conversation_id=:id AND role='assistant'"
                ),
                {"id": conversation},
            )
            == 1
        )


@pytest.mark.parametrize("outcome", ["completed", "release", "uncertain"])
def test_terminal_reservation_cannot_strictly_finalize_again(platform, tenant, outcome):
    _, _, _, _, reservation = owner_claim(platform, tenant)
    with platform["factory"]() as session:
        reconcile_ai_usage(
            session,
            reservation.id,
            outcome=outcome,
            usage=TokenUsage(2, 3, 5, True) if outcome == "completed" else None,
        )
        before = ledger(platform, reservation)
        with pytest.raises(RuntimeError, match="ai_usage_reservation_not_active"):
            reconcile_ai_usage(
                session,
                reservation.id,
                outcome="completed",
                require_new=True,
                usage=TokenUsage(99, 99, 198, True),
            )
        session.rollback()
        assert ledger(platform, reservation) == before


def test_concurrent_owner_finalization_exactly_once(platform, tenant):
    business, _, _, claim, reservation = owner_claim(platform, tenant)
    barrier = Barrier(2)

    def finish():
        with platform["factory"]() as session:
            barrier.wait(timeout=4)
            try:
                _persist_result(
                    session,
                    business,
                    claim,
                    OwnerChatResult(reply="Concurrent result"),
                    reservation,
                    TokenUsage(2, 3, 5, True),
                    (),
                )
                return "completed"
            except ApplicationError:
                session.rollback()
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: finish(), range(2))) == [
            "completed",
            "rejected",
        ]
    assert ledger(platform, reservation) == ("completed", 5, True, 5, 0)


@pytest.mark.parametrize(
    "locked_table,expected",
    [
        ("ai_usage_reservations", "ai_usage_reservation_not_active"),
        ("business_ai_usage_daily", "ai_usage_finalization_deadline_expired"),
    ],
)
@pytest.mark.parametrize("expiry", ["claim", "lease", "window"])
def test_deadline_after_native_lock_wait_rolls_back_response(
    platform, tenant, locked_table, expected, expiry
):
    business, conversation, message, claim, reservation = owner_claim(platform, tenant)
    blocker = platform["admin"].connect()
    transaction = blocker.begin()
    predicate = (
        "id=:id" if locked_table == "ai_usage_reservations" else "business_id=:id"
    )
    blocker.execute(
        text(f"SELECT 1 FROM {locked_table} WHERE {predicate} FOR UPDATE"),
        {"id": reservation.id if locked_table == "ai_usage_reservations" else business},
    )
    if expiry == "claim":
        with platform["admin"].begin() as connection:
            connection.execute(
                text(
                    "UPDATE owner_chat_messages SET generation_claim_expires_at="
                    "clock_timestamp()+interval '2 seconds' WHERE id=:id"
                ),
                {"id": message},
            )
    else:
        field = "lease_expires_at" if expiry == "lease" else "window_end"
        update = text(
            f"UPDATE ai_usage_reservations SET {field}="
            "clock_timestamp()+interval '2 seconds' WHERE id=:id"
        )
        if locked_table == "ai_usage_reservations":
            blocker.execute(update, {"id": reservation.id})
        else:
            with platform["admin"].begin() as connection:
                connection.execute(update, {"id": reservation.id})
    pid = []

    def finish():
        with platform["factory"]() as session:
            pid.append(session.scalar(text("SELECT pg_backend_pid()")))
            try:
                _persist_result(
                    session,
                    business,
                    claim,
                    OwnerChatResult(reply="Late result"),
                    reservation,
                    TokenUsage(2, 3, 5, True),
                    (),
                )
            except RuntimeError as error:
                session.rollback()
                return str(error)
            return "unexpected success"

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(finish)
            until = time.monotonic() + 3
            while not pid and time.monotonic() < until:
                time.sleep(0.01)
            assert pid
            lock_wait(platform, pid[0])
            time.sleep(2.1)
            # Only this disposable expiry fixture changes; no accounting DML.
            transaction.commit()
            assert future.result(timeout=8) == expected
    finally:
        transaction.rollback() if transaction.is_active else None
        blocker.close()
    assert ledger(platform, reservation) == ("reserved", None, None, 0, 50)
    with platform["runtime"].connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM owner_chat_messages "
                    "WHERE conversation_id=:id AND role='assistant'"
                ),
                {"id": conversation},
            )
            == 0
        )


def seed_turns(platform, tenant):
    user, business, _ = tenant
    with platform["factory"]() as session:
        conversation = OwnerConversation(business_id=business, creator_user_id=user.id)
        session.add(conversation)
        session.flush()
        for sequence in range(1, 17, 2):
            owner = OwnerChatMessage(
                conversation_id=conversation.id,
                sequence_number=sequence,
                role="owner",
                content=f"Generated context {uuid.uuid4().hex[:8]}",
                idempotency_key=uuid.uuid4().hex,
                generation_state="completed",
            )
            session.add(owner)
            session.flush()
            session.add(
                OwnerChatMessage(
                    conversation_id=conversation.id,
                    sequence_number=sequence + 1,
                    role="assistant",
                    content=f"Generated answer {sequence}",
                    reply_to_message_id=owner.id,
                )
            )
        session.commit()
        return conversation.id


@pytest.mark.parametrize(
    "outcome", [None, "completed", "reported_failure", "uncertain", "release"]
)
@pytest.mark.parametrize("state", ["failed", "expired_processing"])
def test_native_summary_retry_usage_and_scoping(
    platform, tenant, monkeypatch, outcome, state
):
    conversation = seed_turns(platform, tenant)
    settings = Settings(
        _env_file=None,
        environment="testing",
        conversation_summary_provider="mock",
        customer_chat_provider="mock",
    )
    monkeypatch.setattr(worker, "get_session_factory", lambda: platform["factory"])
    first = worker._claim_summary(conversation, settings)
    assert first and first[2:] == (0, 4)
    assert worker._claim_summary(conversation, settings) is None
    with platform["factory"]() as session:
        summary = session.get(OwnerConversationSummary, first[0])
        reservation = reserve_conversation_summary_usage(
            session,
            summary=summary,
            claim_token=first[1],
            estimated_input_tokens=20,
            max_output_tokens=30,
            lease_seconds=120,
        )
        if outcome:
            reconcile_ai_usage(
                session,
                reservation.id,
                usage=TokenUsage(2, 3, 5, True)
                if outcome in {"completed", "reported_failure"}
                else None,
                outcome=outcome,
            )
        assert summary_attempt_has_usage(session, summary) is (outcome != "release")
        for params in (
            (summary.id, uuid.uuid4(), conversation, 1),
            (summary.id, summary.business_id, uuid.uuid4(), 1),
            (summary.id, summary.business_id, conversation, 2),
        ):
            assert (
                session.scalar(
                    text("SELECT public.sou2ai_summary_attempt_has_usage(:s,:b,:c,:a)"),
                    dict(zip(("s", "b", "c", "a"), params, strict=True)),
                )
                is False
            )
        if state == "failed":
            summary.generation_state = "failed"
            summary.pending_through_sequence_number = None
            summary.generation_claim_token = None
            summary.generation_claim_expires_at = None
        else:
            summary.generation_claim_expires_at = datetime.now(UTC)
        session.commit()
    retried = worker._claim_summary(conversation, settings)
    assert (retried is not None) is (outcome == "release")
    with platform["factory"]() as session:
        summary = session.get(OwnerConversationSummary, first[0])
        assert summary.summarized_through_sequence_number == 0
        assert summary.last_charged_through_sequence_number == 0
        assert summary.generation_attempts == (2 if outcome == "release" else 1)
        if outcome != "release":
            assert (
                summary.last_failure_code == "summary_expired_dispatch_review_required"
            )


def test_native_summary_finalization_checkpoint_and_retry(
    platform, tenant, monkeypatch
):
    conversation = seed_turns(platform, tenant)
    settings = Settings(
        _env_file=None,
        environment="testing",
        conversation_summary_provider="mock",
        customer_chat_provider="mock",
    )
    monkeypatch.setattr(worker, "get_session_factory", lambda: platform["factory"])
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    # Provider is the actual deterministic mock; Redis/RQ execution is excluded.
    worker.process_conversation_summary(str(conversation))
    with platform["factory"]() as session:
        summary = session.scalar(
            select(OwnerConversationSummary).where(
                OwnerConversationSummary.conversation_id == conversation
            )
        )
        assert summary.summary_version == 1
        assert summary.summarized_through_sequence_number == 4
        assert summary.last_charged_through_sequence_number == 4
        content = summary.content
        assert "Generated context" in content and "Generated answer 3" in content
    worker.process_conversation_summary(str(conversation))
    with platform["factory"]() as session:
        summary = session.scalar(
            select(OwnerConversationSummary).where(
                OwnerConversationSummary.conversation_id == conversation
            )
        )
        assert summary.summary_version == 1 and summary.content == content
    with platform["admin"].connect() as connection:
        assert connection.execute(
            text(
                "SELECT count(*),min(status),max(status) "
                "FROM ai_usage_reservations WHERE conversation_summary_id=:s"
            ),
            {"s": summary.id},
        ).one() == (1, "completed", "completed")


def test_summary_concurrent_claims(platform, tenant, monkeypatch):
    conversation = seed_turns(platform, tenant)
    settings = Settings(_env_file=None, conversation_summary_provider="mock")
    monkeypatch.setattr(worker, "get_session_factory", lambda: platform["factory"])
    barrier = Barrier(2)

    def claim():
        barrier.wait(timeout=4)
        return worker._claim_summary(conversation, settings)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sum(result is not None for result in results) == 1
    with platform["factory"]() as session:
        summary = session.scalar(
            select(OwnerConversationSummary).where(
                OwnerConversationSummary.conversation_id == conversation
            )
        )
        assert summary.generation_attempts == 1


def test_summary_retention_failure_preserves_checkpoint_and_blocks_recharge(
    platform, tenant, monkeypatch
):
    from app.agent.owner_chat_provider import ConversationSummaryResult

    conversation = seed_turns(platform, tenant)
    settings = Settings(_env_file=None, conversation_summary_provider="mock")
    monkeypatch.setattr(worker, "get_session_factory", lambda: platform["factory"])
    monkeypatch.setattr(worker, "get_settings", lambda: settings)

    class IncompleteProvider:
        calls = 0

        def estimate_summary_input_tokens(self, request):
            return 20

        def summarize(self, request):
            self.calls += 1
            return ConversationSummaryResult(
                summary="Incomplete retained context",
                usage=TokenUsage(2, 3, 5, True),
                retained_message_count=1,
            )

    provider = IncompleteProvider()
    monkeypatch.setattr(worker, "create_owner_chat_provider", lambda _: provider)
    worker.process_conversation_summary(str(conversation))
    worker.process_conversation_summary(str(conversation))
    assert provider.calls == 1
    with platform["factory"]() as session:
        summary = session.scalar(
            select(OwnerConversationSummary).where(
                OwnerConversationSummary.conversation_id == conversation
            )
        )
        assert summary.content is None and summary.summary_version == 0
        assert summary.summarized_through_sequence_number == 0
        assert summary.last_charged_through_sequence_number == 4
        assert summary.last_failure_code == "summary_checkpoint_already_charged"
    with platform["admin"].connect() as connection:
        assert connection.execute(
            text(
                "SELECT count(*),min(status),sum(total_tokens) "
                "FROM ai_usage_reservations WHERE conversation_summary_id=:s"
            ),
            {"s": summary.id},
        ).one() == (1, "charged", 5)


def test_legacy_summary_preserved(platform, tenant, monkeypatch):
    conversation = seed_turns(platform, tenant)
    with platform["factory"]() as session:
        session.add(
            OwnerConversationSummary(
                business_id=tenant[1],
                conversation_id=conversation,
                content="Previously stored legacy summary",
                summary_version=1,
                summarized_through_sequence_number=2,
                last_charged_through_sequence_number=2,
                provider_identifier="mock",
                model_identifier="deterministic-v1",
            )
        )
        session.commit()
    monkeypatch.setattr(worker, "get_session_factory", lambda: platform["factory"])
    assert (
        worker._claim_summary(
            conversation, Settings(_env_file=None, conversation_summary_provider="mock")
        )
        is None
    )
    with platform["factory"]() as session:
        summary = session.scalar(
            select(OwnerConversationSummary).where(
                OwnerConversationSummary.conversation_id == conversation
            )
        )
        assert summary.content == "Previously stored legacy summary"
        assert (
            summary.summarized_through_sequence_number == 2
            and summary.last_charged_through_sequence_number == 2
        )
        assert summary.last_failure_code == "summary_legacy_review_required"


def test_http_replay_no_second_provider_dispatch(platform, tenant):
    from app.agent.owner_chat_provider import get_owner_chat_provider
    from app.main import app
    from tests.test_owner_chat import CapturingProvider, submit

    provider = CapturingProvider(
        OwnerChatResult(reply="Isolated greeting", usage=TokenUsage(2, 3, 5, True))
    )
    app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    user, business, client = tenant
    first = submit(client, user, business, "hi", key="isolated-replay")
    assert first.status_code == 200, first.text
    second = submit(client, user, business, "hi", key="isolated-replay")
    assert second.status_code == 200 and second.json()["replayed"] is True
    assert (
        first.json()["assistant_message"]["id"]
        == second.json()["assistant_message"]["id"]
    )
    assert len(provider.requests) == 1
    with platform["admin"].connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM ai_usage_reservations WHERE business_id=:b"),
                {"b": business},
            )
            == 1
        )


def test_http_foreign_tenant_cannot_dispatch_or_charge(platform, tenant):
    from app.agent.owner_chat_provider import get_owner_chat_provider
    from app.main import app
    from tests.test_business_api import create_user
    from tests.test_owner_chat import CapturingProvider, submit

    provider = CapturingProvider()
    app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    _, business, client = tenant
    with platform["factory"]() as session:
        foreign = create_user(session, f"{uuid.uuid4().hex}@example.invalid")
    response = submit(client, foreign, business, "hi", key="foreign-request")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "business_not_found"
    assert not provider.requests
    with platform["admin"].connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM ai_usage_reservations WHERE business_id=:b"),
                {"b": business},
            )
            == 0
        )


@pytest.mark.parametrize(
    "outcome", ["completed", "pre_use", "uncertain", "reported", "late"]
)
def test_customer_native_finalization_failures_and_replay(
    platform, tenant, monkeypatch, outcome
):
    from app.agent.owner_chat_provider import OwnerChatProviderError
    from app.worker import customer_messages
    from tests.test_owner_chat import CapturingProvider

    business = tenant[1]
    with platform["factory"]() as session:
        channel = MessagingChannelConnection(
            business_id=business,
            provider_type="meta_whatsapp",
            display_name="Disposable channel",
            connection_profile_key="disposable_test",
            external_phone_number_id=uuid.uuid4().hex,
            status="ACTIVE",
            auto_reply_enabled=True,
            last_validated_at=datetime.now(UTC),
            last_successful_health_check_at=datetime.now(UTC),
        )
        session.add(channel)
        session.flush()
        conversation = CustomerConversation(
            business_id=business,
            connection_id=channel.id,
            customer_identity_hash=uuid.uuid4().hex * 2,
            encrypted_customer_identity="Synthetic unused ciphertext",
            masked_customer_label="***0000",
            state="AI_ACTIVE",
        )
        session.add(conversation)
        session.flush()
        message = CustomerMessage(
            business_id=business,
            conversation_id=conversation.id,
            direction="inbound",
            sender="customer",
            content="hi",
            provider_message_id=uuid.uuid4().hex,
            status="RECEIVED",
        )
        session.add(message)
        session.commit()
        identifier = message.id

    class ControlledProvider(CapturingProvider):
        def generate(self, request):
            if outcome in {"pre_use", "uncertain", "reported"}:
                self.requests.append(request)
                raise OwnerChatProviderError(
                    reason="isolated_provider_failure",
                    usage_uncertain=outcome == "uncertain",
                    usage=TokenUsage(2, 3, 5, True) if outcome == "reported" else None,
                )
            if outcome == "late":
                with platform["admin"].begin() as connection:
                    connection.execute(
                        text(
                            "UPDATE customer_messages SET claim_expires_at="
                            "clock_timestamp() "
                            "WHERE id=:id"
                        ),
                        {"id": identifier},
                    )
            return super().generate(request)

    provider = ControlledProvider(
        OwnerChatResult(reply="Disposable reply", usage=TokenUsage(2, 3, 5, True)),
        input_estimate=20,
    )
    settings = Settings(
        _env_file=None,
        environment="testing",
        customer_chat_provider="mock",
        conversation_summary_provider="mock",
        customer_chat_max_output_tokens=30,
    )
    monkeypatch.setattr(
        customer_messages, "get_session_factory", lambda: platform["factory"]
    )
    monkeypatch.setattr(
        customer_messages, "enqueue_outbound_message", lambda *args: None
    )
    # Real claim/recovery/retrieval/admission/reservation/finalization SQL. No documents
    # exist, so only the provider and outbound RQ are controlled.
    customer_messages.process_inbound_message(
        str(identifier), provider=provider, settings_override=settings
    )
    customer_messages.process_inbound_message(
        str(identifier), provider=provider, settings_override=settings
    )
    assert len(provider.requests) == 1
    with platform["admin"].connect() as connection:
        usage = connection.execute(
            text(
                "SELECT status,total_tokens,counts_authoritative "
                "FROM ai_usage_reservations "
                "WHERE customer_message_id=:id"
            ),
            {"id": identifier},
        ).one()
        assert usage.status == (
            "completed"
            if outcome == "completed"
            else "released"
            if outcome == "pre_use"
            else "charged"
        )
        assert usage.total_tokens == (
            5
            if outcome in {"completed", "reported", "late"}
            else 0
            if outcome == "pre_use"
            else 50
        )
        assert usage.counts_authoritative is (
            outcome in {"completed", "reported", "late"}
        )
        replies = connection.scalar(
            text(
                "SELECT count(*) FROM customer_messages WHERE reply_to_message_id=:id"
            ),
            {"id": identifier},
        )
        assert replies == (1 if outcome == "completed" else 0)
        assert (
            connection.scalar(
                text(
                    "SELECT tokens_reserved FROM business_ai_usage_daily "
                    "WHERE business_id=:b"
                ),
                {"b": business},
            )
            == 0
        )


def test_z_downgrade_reupgrade_preserves_records_and_old_contracts(platform):
    def data():
        with platform["admin"].connect() as connection:
            return tuple(
                connection.execute(
                    text(
                        f"SELECT row_to_json(t)::text FROM public.{table} t ORDER BY 1"
                    )
                )
                .scalars()
                .all()
                for table in (
                    "ai_usage_reservations",
                    "business_ai_usage_daily",
                    "owner_conversation_summaries",
                    "owner_chat_messages",
                    "business_ai_allowance_configs",
                    "business_lifecycle_history",
                )
            )

    before = data()
    os.environ["PGOPTIONS"] = "-c role=sou2ai_migrator"
    try:
        command.downgrade(platform["config"], "20261007_16")
        assert catalog_snapshot(platform["admin"]) == platform["before"]
        assert data() == before
        command.upgrade(platform["config"], "20261013_17")
        assert data() == before
    finally:
        os.environ.pop("PGOPTIONS")
