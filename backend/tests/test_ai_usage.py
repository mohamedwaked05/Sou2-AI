"""Per-business AI allowance, reservation, ACL, and summary tests."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.agent.owner_chat_provider import (
    OwnerChatProviderInvalidResponse,
    OwnerChatProviderUnavailable,
    OwnerChatRequest,
    OwnerChatResult,
    TokenUsage,
    get_owner_chat_provider,
)
from app.database.models import (
    Business,
    ChatGenerationState,
    ChatMessageRole,
    OwnerChatMessage,
    OwnerConversation,
)
from app.main import app
from app.services.ai_usage import business_local_day_window
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from tests.test_business_api import headers
from tests.test_owner_chat import CapturingProvider, active_business, submit


def change_allowance(
    operator_engine: Engine,
    business_id: object,
    allowance: int,
    reserve_percent: int = 25,
    *,
    reason: str = "Test allowance adjustment",
) -> None:
    with operator_engine.begin() as connection:
        connection.execute(
            text(
                "SELECT * FROM public.sou2ai_change_business_ai_allowance("
                ":business_id, :allowance, :reserve, :admin, :reason)"
            ),
            {
                "business_id": business_id,
                "allowance": allowance,
                "reserve": reserve_percent,
                "admin": "security-test@example.com",
                "reason": reason,
            },
        ).one()


def test_new_business_gets_default_allowance_and_empty_usage_summary(
    api_client: TestClient,
    db_session: Session,
    migration_engine: Engine,
) -> None:
    user, business = active_business(api_client, db_session)
    with migration_engine.connect() as connection:
        config = connection.execute(
            text(
                "SELECT * FROM business_ai_allowance_configs "
                "WHERE business_id = :business_id"
            ),
            {"business_id": business["id"]},
        ).one()
    assert config.daily_token_allowance == 20_000
    assert config.owner_reserve_percent == 25

    response = api_client.get(
        f"/api/v1/businesses/{business['id']}/ai-usage/current",
        headers=headers(user),
    )
    assert response.status_code == 200
    assert response.json()["daily_token_allowance"] == 20_000
    assert response.json()["owner_reserved_tokens"] == 5_000
    assert response.json()["total_tokens_used"] == 0
    assert response.json()["tokens_remaining"] == 20_000
    assert response.json()["status"] == "normal"


def test_authoritative_usage_reconciles_once_and_replay_is_not_charged_twice(
    api_client: TestClient, db_session: Session, migration_engine: Engine
) -> None:
    user, business = active_business(api_client, db_session)
    result = OwnerChatResult(
        reply="Authoritative response.",
        usage=TokenUsage(120, 30, 150, True),
        provider_identifier="test-provider",
        model_identifier="test-model",
    )
    provider = CapturingProvider(result)
    app.dependency_overrides[get_owner_chat_provider] = lambda: provider

    first = submit(api_client, user, business["id"], "usage question", key="usage-1")
    replay = submit(api_client, user, business["id"], "usage question", key="usage-1")
    assert first.status_code == replay.status_code == 200
    assert replay.json()["replayed"] is True

    with migration_engine.connect() as connection:
        summary = connection.execute(
            text(
                "SELECT * FROM business_ai_usage_daily WHERE business_id = :business_id"
            ),
            {"business_id": business["id"]},
        ).one()
        reservations = connection.scalar(
            text(
                "SELECT count(*) FROM ai_usage_reservations "
                "WHERE business_id = :business_id"
            ),
            {"business_id": business["id"]},
        )
    assert (summary.input_tokens_used, summary.output_tokens_used) == (120, 30)
    assert summary.total_tokens_used == 150
    assert summary.tokens_reserved == 0
    assert reservations == 1
    assert len(provider.requests) == 1


def test_insufficient_budget_blocks_before_provider_and_keeps_turn_retryable(
    api_client: TestClient,
    db_session: Session,
    operator_engine: Engine,
    migration_engine: Engine,
) -> None:
    user, business = active_business(api_client, db_session)
    change_allowance(operator_engine, business["id"], 1)
    provider = CapturingProvider()
    app.dependency_overrides[get_owner_chat_provider] = lambda: provider

    blocked = submit(
        api_client, user, business["id"], "budget blocked", key="budget-blocked"
    )
    repeated = submit(
        api_client, user, business["id"], "budget blocked", key="budget-blocked"
    )

    assert blocked.status_code == repeated.status_code == 429
    assert blocked.json()["error"]["code"] == "daily_ai_token_limit_reached"
    assert int(blocked.headers["retry-after"]) > 0
    assert provider.requests == []
    with migration_engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT count(*) FROM ai_usage_reservations")) == 0
        )
        owner_rows = connection.execute(
            text(
                "SELECT generation_state, generation_attempts FROM owner_chat_messages "
                "WHERE content = 'budget blocked'"
            )
        ).all()
    assert owner_rows == [("failed", 0)]


class BeforeUseFailureProvider:
    def generate(self, request: OwnerChatRequest) -> OwnerChatResult:
        raise OwnerChatProviderUnavailable(usage_uncertain=False)


class ReportedFailureProvider:
    def generate(self, request: OwnerChatRequest) -> OwnerChatResult:
        raise OwnerChatProviderInvalidResponse(
            usage=TokenUsage(75, 10, 85, True),
            provider_identifier="test-provider",
            model_identifier="test-model",
        )


@pytest.mark.parametrize(
    ("provider", "expected_status", "expected_total"),
    [
        (BeforeUseFailureProvider(), "released", 0),
        (ReportedFailureProvider(), "charged", 85),
    ],
)
def test_provider_failures_release_or_charge_reported_usage(
    api_client: TestClient,
    db_session: Session,
    migration_engine: Engine,
    provider,
    expected_status: str,
    expected_total: int,
) -> None:
    user, business = active_business(api_client, db_session)
    app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    response = submit(api_client, user, business["id"], "provider failure")
    assert response.status_code == 503
    with migration_engine.connect() as connection:
        reservation = connection.execute(
            text("SELECT * FROM ai_usage_reservations")
        ).one()
        summary = connection.execute(
            text("SELECT * FROM business_ai_usage_daily")
        ).one()
    assert reservation.status == expected_status
    assert reservation.total_tokens == expected_total
    assert summary.total_tokens_used == expected_total
    assert summary.tokens_reserved == 0


def test_allowance_operator_audit_and_direct_mutations_are_restricted(
    api_client: TestClient,
    db_session: Session,
    database_engine: Engine,
    operator_engine: Engine,
    migration_engine: Engine,
) -> None:
    _user, business = active_business(api_client, db_session)
    injection_reason = "'; DROP TABLE businesses; --"
    change_allowance(
        operator_engine,
        business["id"],
        30_000,
        20,
        reason=injection_reason,
    )
    with migration_engine.connect() as connection:
        audit = connection.execute(
            text("SELECT * FROM business_ai_allowance_audit")
        ).one()
        assert audit.reason == injection_reason
        assert connection.scalar(text("SELECT to_regclass('public.businesses')"))
        signature = (
            "public.sou2ai_change_business_ai_allowance(uuid,integer,integer,text,text)"
        )
        assert (
            connection.scalar(
                text(
                    "SELECT pg_get_userbyid(proowner) FROM pg_proc "
                    "WHERE oid=CAST(:sig AS regprocedure)"
                ),
                {"sig": signature},
            )
            == "sou2ai_migrator"
        )

    for engine in (database_engine, operator_engine):
        with engine.connect() as connection, pytest.raises(ProgrammingError):
            connection.execute(
                text(
                    "UPDATE business_ai_allowance_configs "
                    "SET daily_token_allowance = 2 WHERE business_id = :id"
                ),
                {"id": business["id"]},
            )
    with database_engine.connect() as connection, pytest.raises(ProgrammingError):
        connection.execute(
            text(
                "SELECT * FROM public.sou2ai_change_business_ai_allowance("
                ":id, 2, 25, 'runtime', 'forbidden')"
            ),
            {"id": business["id"]},
        )
    for statement in (
        "INSERT INTO business_ai_allowance_audit "
        "(id,business_id,previous_daily_token_allowance,new_daily_token_allowance,"
        "previous_owner_reserve_percent,new_owner_reserve_percent,"
        "admin_identifier,reason) "
        "VALUES (gen_random_uuid(),:id,1,2,25,25,'x','x')",
        "UPDATE business_ai_allowance_audit SET reason='x' WHERE business_id=:id",
        "DELETE FROM business_ai_allowance_audit WHERE business_id=:id",
        "TRUNCATE business_ai_allowance_audit",
    ):
        with operator_engine.connect() as connection, pytest.raises(ProgrammingError):
            connection.execute(text(statement), {"id": business["id"]})


def test_budget_function_owners_search_paths_and_execution_acls(
    migration_engine: Engine,
) -> None:
    reserve_signature = (
        "public.sou2ai_reserve_ai_usage(uuid,uuid,uuid,integer,text,text,"
        "integer,integer,integer)"
    )
    admin_signature = (
        "public.sou2ai_change_business_ai_allowance(uuid,integer,integer,text,text)"
    )
    with migration_engine.connect() as connection:
        for signature in (reserve_signature, admin_signature):
            function = connection.execute(
                text(
                    "SELECT p.prosecdef,p.proconfig,pg_get_userbyid(p.proowner) owner "
                    "FROM pg_proc p WHERE p.oid=CAST(:signature AS regprocedure)"
                ),
                {"signature": signature},
            ).one()
            assert function.prosecdef is True
            assert function.proconfig == ["search_path=pg_catalog"]
            assert function.owner == "sou2ai_migrator"
            assert (
                connection.scalar(
                    text(
                        "SELECT has_function_privilege('public',:signature,'EXECUTE')"
                    ),
                    {"signature": signature},
                )
                is False
            )
        assert (
            connection.scalar(
                text(
                    "SELECT has_function_privilege("
                    "'sou2ai_runtime',:signature,'EXECUTE')"
                ),
                {"signature": reserve_signature},
            )
            is True
        )
        assert (
            connection.scalar(
                text(
                    "SELECT has_function_privilege("
                    "'sou2ai_runtime',:signature,'EXECUTE')"
                ),
                {"signature": admin_signature},
            )
            is False
        )
        assert (
            connection.scalar(
                text(
                    "SELECT has_function_privilege('sou2ai_lifecycle_operator',"
                    ":signature,'EXECUTE')"
                ),
                {"signature": admin_signature},
            )
            is True
        )
        assert (
            connection.scalar(
                text(
                    "SELECT has_function_privilege('sou2ai_lifecycle_operator',"
                    ":signature,'EXECUTE')"
                ),
                {"signature": reserve_signature},
            )
            is False
        )


def test_business_local_windows_preserve_midnight_across_dst() -> None:
    business = Business(owner_user_id=uuid.uuid4(), name="Timezone Market")
    business.timezone = "Asia/Beirut"
    timezone = ZoneInfo("Asia/Beirut")
    for moment in (
        datetime(2026, 3, 29, 12, tzinfo=UTC),
        datetime(2026, 10, 25, 12, tzinfo=UTC),
    ):
        start, end = business_local_day_window(business, moment=moment)
        local_start = start.astimezone(timezone)
        local_end = end.astimezone(timezone)
        assert local_start.date() == moment.astimezone(timezone).date()
        assert local_end.date() == local_start.date() + timedelta(days=1)
        assert 23 * 3600 <= (end - start).total_seconds() <= 25 * 3600


def test_concurrent_reservations_cannot_exceed_allowance(
    api_client: TestClient,
    db_session: Session,
    database_engine: Engine,
    operator_engine: Engine,
    migration_engine: Engine,
) -> None:
    user, business_payload = active_business(api_client, db_session)
    user_id = user.id
    business_id = uuid.UUID(str(business_payload["id"]))
    change_allowance(operator_engine, business_id, 1_000)
    conversation = db_session.scalar(
        select(OwnerConversation).where(OwnerConversation.business_id == business_id)
    )
    messages = [
        OwnerChatMessage(
            conversation_id=conversation.id,
            sequence_number=1001 + index * 2,
            role=ChatMessageRole.OWNER,
            content=f"reservation {index}",
            idempotency_key=f"reservation-{index}",
            generation_state=ChatGenerationState.PROCESSING,
            generation_claim_token=uuid.uuid4(),
            generation_claim_expires_at=datetime.now(UTC) + timedelta(seconds=150),
            generation_attempts=1,
        )
        for index in range(2)
    ]
    db_session.add_all(messages)
    db_session.commit()

    def reserve(message_id: uuid.UUID) -> str:
        factory = sessionmaker(bind=database_engine)
        with factory() as session:
            try:
                session.execute(
                    text(
                        "SELECT * FROM public.sou2ai_reserve_ai_usage("
                        ":business_id,:user_id,:message_id,1,'owner','owner_chat',"
                        "300,500,150)"
                    ),
                    {
                        "business_id": business_id,
                        "user_id": user_id,
                        "message_id": message_id,
                    },
                ).one()
                session.commit()
                return "reserved"
            except DBAPIError as exc:
                session.rollback()
                assert "daily_ai_token_limit_reached" in str(exc.orig)
                return "blocked"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, [message.id for message in messages]))
    assert sorted(results) == ["blocked", "reserved"]
    with migration_engine.connect() as connection:
        summary = connection.execute(
            text(
                "SELECT * FROM business_ai_usage_daily WHERE business_id=:business_id"
            ),
            {"business_id": business_id},
        ).one()
    assert summary.tokens_reserved == 800
    assert summary.total_tokens_used == 0
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ai_usage_reservations "
                "SET created_at=now()-interval '10 seconds', "
                "lease_expires_at=now()-interval '1 second' "
                "WHERE business_id=:business_id AND status='reserved'"
            ),
            {"business_id": business_id},
        )
    refreshed = api_client.get(
        f"/api/v1/businesses/{business_id}/ai-usage/current",
        headers=headers(user),
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["total_tokens_used"] == 800
    assert refreshed.json()["tokens_currently_reserved"] == 0
    with migration_engine.connect() as connection:
        charged = connection.execute(
            text(
                "SELECT * FROM ai_usage_reservations "
                "WHERE business_id=:business_id AND status='charged'"
            ),
            {"business_id": business_id},
        ).one()
    assert charged.counts_authoritative is False


def test_usage_endpoint_tenant_isolation_and_thresholds(
    api_client: TestClient,
    db_session: Session,
    migration_engine: Engine,
) -> None:
    owner, business = active_business(api_client, db_session)
    foreign, _other = active_business(
        api_client,
        db_session,
        email="usage-foreign@example.com",
        name="Usage Foreign Market",
    )
    hidden = api_client.get(
        f"/api/v1/businesses/{business['id']}/ai-usage/current",
        headers=headers(foreign),
    )
    assert hidden.status_code == 404

    stored_business = db_session.get(Business, uuid.UUID(str(business["id"])))
    window_start, window_end = business_local_day_window(stored_business)
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO business_ai_usage_daily "
                "(id,business_id,window_start,window_end,input_tokens_used,"
                "output_tokens_used,total_tokens_used,tokens_reserved) "
                "VALUES (gen_random_uuid(),:business_id,:window_start,"
                ":window_end,10000,5000,15000,0) "
                "ON CONFLICT (business_id,window_start) DO UPDATE SET "
                "input_tokens_used=10000,output_tokens_used=5000,"
                "total_tokens_used=15000,tokens_reserved=0"
            ),
            {
                "business_id": business["id"],
                "window_start": window_start,
                "window_end": window_end,
            },
        )
    visible = api_client.get(
        f"/api/v1/businesses/{business['id']}/ai-usage/current",
        headers=headers(owner),
    )
    assert visible.status_code == 200
    assert visible.json()["usage_percentage"] == 75.0
    assert visible.json()["status"] == "approaching_limit"


def test_security_retention_uses_24h_48h_90d_and_12_month_cutoffs(
    api_client: TestClient,
    db_session: Session,
    migration_engine: Engine,
) -> None:
    _user, business = active_business(api_client, db_session)
    business_id = business["id"]
    conversation = db_session.scalar(
        select(OwnerConversation).where(
            OwnerConversation.business_id == uuid.UUID(str(business_id))
        )
    )
    marker = OwnerChatMessage(
        conversation_id=conversation.id,
        sequence_number=2001,
        role=ChatMessageRole.OWNER,
        content="retention marker",
        idempotency_key="retention-marker",
        generation_state=ChatGenerationState.FAILED,
    )
    db_session.add(marker)
    db_session.commit()
    old_start = datetime.now(UTC) - timedelta(days=500)
    old_end = old_start + timedelta(days=1)
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO registration_rate_limit_events "
                "(id,normalized_email,client_ip,created_at) VALUES "
                "(gen_random_uuid(),'old@example.com','127.0.0.1',"
                "now()-interval '49 hours'),"
                "(gen_random_uuid(),'new@example.com','127.0.0.1',"
                "now()-interval '47 hours')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO owner_chat_rate_limit_events "
                "(id,business_id,owner_message_id,generation_attempt,created_at) "
                "VALUES (gen_random_uuid(),:business_id,:message_id,100,"
                "now()-interval '25 hours'),"
                "(gen_random_uuid(),:business_id,:message_id,101,"
                "now()-interval '23 hours')"
            ),
            {"business_id": business_id, "message_id": marker.id},
        )
        summary_id = connection.scalar(
            text(
                "INSERT INTO business_ai_usage_daily "
                "(id,business_id,window_start,window_end) "
                "VALUES (gen_random_uuid(),:business_id,:start,:end) RETURNING id"
            ),
            {"business_id": business_id, "start": old_start, "end": old_end},
        )
        connection.execute(
            text(
                "INSERT INTO ai_usage_reservations "
                "(id,business_id,generation_attempt,channel,capability,"
                "estimated_input_tokens,max_output_tokens,reserved_tokens,"
                "input_tokens,output_tokens,total_tokens,counts_authoritative,status,"
                "window_start,window_end,lease_expires_at,created_at,reconciled_at) "
                "VALUES (gen_random_uuid(),:business_id,1,'owner','owner_chat',"
                "0,1,1,0,0,0,false,'released',:start,:end,now()-interval '90 days',"
                "now()-interval '91 days',now()-interval '90 days')"
            ),
            {"business_id": business_id, "start": old_start, "end": old_end},
        )
    result = db_session.execute(
        text("SELECT * FROM public.sou2ai_cleanup_security_records(now(),1000)")
    ).one()
    db_session.commit()
    assert tuple(result) == (1, 1, 1, 1)
    with migration_engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM registration_rate_limit_events")
            )
            == 1
        )
        assert (
            connection.scalar(text("SELECT count(*) FROM owner_chat_rate_limit_events"))
            == 1
        )
        assert (
            connection.scalar(text("SELECT count(*) FROM ai_usage_reservations")) == 0
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM business_ai_usage_daily WHERE id=:id"),
                {"id": summary_id},
            )
            == 0
        )
