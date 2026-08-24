"""Persistent registration and owner-chat request-limit tests."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from app.agent.owner_chat_provider import get_owner_chat_provider
from app.core.security import utc_now
from app.database.models import (
    ChatGenerationState,
    ChatMessageRole,
    OwnerChatMessage,
    OwnerChatRateLimitEvent,
    OwnerConversation,
    RegistrationRateLimitEvent,
)
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from tests.test_auth_registration import register
from tests.test_owner_chat import CapturingProvider, active_business, submit


def test_registration_email_limit_counts_successes_and_failures(
    api_client: TestClient, migration_engine: Engine
) -> None:
    assert register(api_client).status_code == 201
    for _ in range(4):
        assert register(api_client).status_code == 409

    blocked = register(api_client)

    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "registration_email_rate_limited"
    assert int(blocked.headers["retry-after"]) > 0
    assert blocked.json()["error"]["request_id"] == blocked.headers["x-request-id"]
    with migration_engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count()).select_from(RegistrationRateLimitEvent)
            )
            == 5
        )


def test_registration_ip_windows_and_shared_ip_below_ceiling(
    api_client: TestClient, migration_engine: Engine
) -> None:
    now = utc_now()
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO registration_rate_limit_events "
                "(id,normalized_email,client_ip,created_at) "
                "VALUES (gen_random_uuid(),:email,'127.0.0.1',:created_at)"
            ),
            [
                {
                    "email": f"prior-{index}@example.com",
                    "created_at": now - timedelta(minutes=1),
                }
                for index in range(29)
            ],
        )
    allowed = register(api_client, email="shared-ip@example.com")
    assert allowed.status_code == 201

    blocked = register(api_client, email="next-shared-ip@example.com")
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "registration_ip_rate_limited"

    with migration_engine.begin() as connection:
        connection.execute(
            text("UPDATE registration_rate_limit_events SET created_at=:created_at"),
            {"created_at": now - timedelta(hours=1)},
        )
        existing_count = connection.scalar(
            select(func.count()).select_from(RegistrationRateLimitEvent)
        )
        connection.execute(
            text(
                "INSERT INTO registration_rate_limit_events "
                "(id,normalized_email,client_ip,created_at) "
                "VALUES (gen_random_uuid(),:email,'127.0.0.1',:created_at)"
            ),
            [
                {
                    "email": f"daily-{index}@example.com",
                    "created_at": now - timedelta(hours=1),
                }
                for index in range(100 - existing_count)
            ],
        )
    daily = register(api_client, email="daily-blocked@example.com")
    assert daily.status_code == 429
    assert daily.json()["error"]["code"] == "registration_ip_daily_rate_limited"


def test_registration_admission_precedes_password_hashing_and_delivery(
    api_client: TestClient,
    migration_engine: Engine,
    monkeypatch,
) -> None:
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO registration_rate_limit_events "
                "(id,normalized_email,client_ip) VALUES "
                "(gen_random_uuid(),'blocked@example.com','127.0.0.1')"
            ),
            [{} for _ in range(5)],
        )
    monkeypatch.setattr(
        "app.services.auth.hash_password",
        lambda _value: (_ for _ in ()).throw(AssertionError("hash called")),
    )

    blocked = register(api_client, email="blocked@example.com")

    assert blocked.status_code == 429


def test_owner_chat_minute_limit_and_blocked_idempotent_retry(
    api_client: TestClient, db_session: Session, migration_engine: Engine
) -> None:
    user, business = active_business(api_client, db_session)
    provider = CapturingProvider()
    app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    for index in range(3):
        response = submit(
            api_client,
            user,
            business["id"],
            f"allowed-{index}",
            key=f"rate-{index}",
        )
        assert response.status_code == 200

    blocked = submit(
        api_client,
        user,
        business["id"],
        "blocked generation",
        key="rate-blocked",
    )
    repeated = submit(
        api_client,
        user,
        business["id"],
        "blocked generation",
        key="rate-blocked",
    )

    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "owner_chat_rate_limited"
    assert repeated.status_code == 409
    assert repeated.json()["error"]["code"] == "owner_turn_failed"
    assert len(provider.requests) == 3
    with migration_engine.connect() as connection:
        assert (
            connection.scalar(select(func.count()).select_from(OwnerChatRateLimitEvent))
            == 3
        )
    blocked_messages = db_session.scalars(
        select(OwnerChatMessage).where(OwnerChatMessage.content == "blocked generation")
    ).all()
    assert len(blocked_messages) == 1
    assert blocked_messages[0].generation_state == ChatGenerationState.FAILED
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(OwnerChatMessage)
            .where(OwnerChatMessage.reply_to_message_id == blocked_messages[0].id)
        )
        == 0
    )


def test_owner_chat_hour_limit_and_business_isolation(
    api_client: TestClient, db_session: Session, migration_engine: Engine
) -> None:
    user, business = active_business(api_client, db_session, name="Hourly Market")
    other_user, other = active_business(
        api_client,
        db_session,
        email="other-hourly@example.com",
        name="Other Market",
    )
    conversation = db_session.scalar(
        select(OwnerConversation).where(
            OwnerConversation.business_id == uuid.UUID(business["id"])
        )
    )
    marker = OwnerChatMessage(
        conversation_id=conversation.id,
        sequence_number=999,
        role=ChatMessageRole.OWNER,
        content="hourly marker",
        idempotency_key="hourly-marker",
        generation_state=ChatGenerationState.FAILED,
    )
    db_session.add(marker)
    db_session.flush()
    db_session.commit()
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO owner_chat_rate_limit_events "
                "(id,business_id,owner_message_id,generation_attempt,created_at) "
                "VALUES (gen_random_uuid(),:business_id,:message_id,:attempt,"
                ":created_at)"
            ),
            [
                {
                    "business_id": conversation.business_id,
                    "message_id": marker.id,
                    "attempt": index + 1,
                    "created_at": utc_now() - timedelta(minutes=2),
                }
                for index in range(20)
            ],
        )

    blocked = submit(
        api_client,
        user,
        business["id"],
        "hourly blocked",
        key="hourly-blocked",
    )
    allowed_other = submit(
        api_client,
        other_user,
        other["id"],
        "isolated allowance",
        key="other-allowed",
    )

    assert blocked.status_code == 429
    assert allowed_other.status_code == 200


def test_runtime_cannot_mutate_or_read_rate_event_tables(
    database_engine: Engine, migration_engine: Engine
) -> None:
    statements = (
        "INSERT INTO registration_rate_limit_events "
        "(id,normalized_email,client_ip) VALUES "
        "(gen_random_uuid(),'forged@example.com','127.0.0.1')",
        "DELETE FROM registration_rate_limit_events",
        "INSERT INTO owner_chat_rate_limit_events "
        "(id,business_id,owner_message_id,generation_attempt) VALUES "
        "(gen_random_uuid(),gen_random_uuid(),gen_random_uuid(),1)",
        "DELETE FROM owner_chat_rate_limit_events",
    )
    for statement in statements:
        with database_engine.connect() as connection, pytest.raises(ProgrammingError):
            connection.execute(text(statement))
    with database_engine.connect() as connection, pytest.raises(ProgrammingError):
        connection.execute(
            text(
                "SELECT * FROM public.sou2ai_cleanup_security_records("
                "clock_timestamp(),1)"
            )
        )
    with migration_engine.connect() as connection:
        for table in (
            "registration_rate_limit_events",
            "owner_chat_rate_limit_events",
        ):
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                assert (
                    connection.scalar(
                        text(
                            "SELECT has_table_privilege("
                            "'sou2ai_runtime',:table,:privilege)"
                        ),
                        {"table": f"public.{table}", "privilege": privilege},
                    )
                    is False
                )


def test_rate_function_owners_search_paths_and_execution_acls(
    migration_engine: Engine,
) -> None:
    signatures = (
        "public.sou2ai_admit_registration_attempt(text,text)",
        "public.sou2ai_admit_owner_chat_generation(uuid,uuid,integer)",
        "public.sou2ai_undo_owner_chat_generation_admission(uuid,uuid,integer,uuid)",
        "public.sou2ai_cleanup_security_records(integer)",
    )
    with migration_engine.connect() as connection:
        for signature in signatures:
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
                        "SELECT has_function_privilege("
                        "'sou2ai_runtime',:signature,'EXECUTE')"
                    ),
                    {"signature": signature},
                )
                is True
            )
            for denied_role in ("public", "sou2ai_lifecycle_operator"):
                assert (
                    connection.scalar(
                        text(
                            "SELECT has_function_privilege(:role,:signature,'EXECUTE')"
                        ),
                        {"role": denied_role, "signature": signature},
                    )
                    is False
                )
        assert (
            connection.scalar(
                text(
                    "SELECT to_regprocedure("
                    "'public.sou2ai_cleanup_security_records(timestamptz,integer)')"
                )
            )
            is None
        )


def test_concurrent_registration_admission_enforces_exact_email_limit(
    database_engine: Engine, migration_engine: Engine
) -> None:
    factory = sessionmaker(bind=database_engine)

    def admit(index: int) -> bool:
        with factory() as session:
            row = session.execute(
                text(
                    "SELECT * FROM public.sou2ai_admit_registration_attempt("
                    ":email,:client_ip)"
                ),
                {
                    "email": "concurrent@example.com",
                    "client_ip": f"198.51.100.{index + 1}",
                },
            ).one()
            session.commit()
            return bool(row.admitted)

    with ThreadPoolExecutor(max_workers=6) as executor:
        admitted = list(executor.map(admit, range(6)))
    assert admitted.count(True) == 5
    assert admitted.count(False) == 1
    with migration_engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM registration_rate_limit_events "
                    "WHERE normalized_email='concurrent@example.com'"
                )
            )
            == 5
        )


def test_concurrent_owner_admission_enforces_exact_minute_limit(
    api_client: TestClient,
    db_session: Session,
    database_engine: Engine,
    migration_engine: Engine,
) -> None:
    _user, business = active_business(api_client, db_session)
    business_id = uuid.UUID(str(business["id"]))
    conversation = db_session.scalar(
        select(OwnerConversation).where(OwnerConversation.business_id == business_id)
    )
    messages = [
        OwnerChatMessage(
            conversation_id=conversation.id,
            sequence_number=1001 + index * 2,
            role=ChatMessageRole.OWNER,
            content=f"concurrent rate {index}",
            idempotency_key=f"concurrent-rate-{index}",
            generation_state=ChatGenerationState.PENDING,
        )
        for index in range(4)
    ]
    db_session.add_all(messages)
    db_session.commit()
    message_ids = [message.id for message in messages]
    factory = sessionmaker(bind=database_engine)

    def admit(message_id: uuid.UUID) -> bool:
        with factory() as session:
            row = session.execute(
                text(
                    "SELECT * FROM public.sou2ai_admit_owner_chat_generation("
                    ":business_id,:message_id,1)"
                ),
                {"business_id": business_id, "message_id": message_id},
            ).one()
            session.commit()
            return bool(row.admitted)

    with ThreadPoolExecutor(max_workers=4) as executor:
        admitted = list(executor.map(admit, message_ids))
    assert admitted.count(True) == 3
    assert admitted.count(False) == 1
    with migration_engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM owner_chat_rate_limit_events "
                    "WHERE business_id=:business_id"
                ),
                {"business_id": business_id},
            )
            == 3
        )


def test_controlled_owner_admission_undo_checks_claim_message_and_reservation(
    api_client: TestClient,
    db_session: Session,
    database_engine: Engine,
    migration_engine: Engine,
) -> None:
    user, business = active_business(api_client, db_session)
    user_id = user.id
    business_id = uuid.UUID(str(business["id"]))
    conversation = db_session.scalar(
        select(OwnerConversation).where(OwnerConversation.business_id == business_id)
    )
    tokens = [uuid.uuid4(), uuid.uuid4()]
    messages = [
        OwnerChatMessage(
            conversation_id=conversation.id,
            sequence_number=2001 + index * 2,
            role=ChatMessageRole.OWNER,
            content=f"undo target {index}",
            idempotency_key=f"undo-target-{index}",
            generation_state=ChatGenerationState.PENDING,
        )
        for index in range(2)
    ]
    db_session.add_all(messages)
    db_session.commit()
    with database_engine.begin() as connection:
        for message in messages:
            assert (
                connection.execute(
                    text(
                        "SELECT * FROM public.sou2ai_admit_owner_chat_generation("
                        ":business_id,:message_id,1)"
                    ),
                    {"business_id": business_id, "message_id": message.id},
                )
                .one()
                .admitted
            )
    with migration_engine.begin() as connection:
        for message, token in zip(messages, tokens, strict=True):
            connection.execute(
                text(
                    "UPDATE owner_chat_messages SET generation_state='processing',"
                    "generation_attempts=1,generation_claim_token=:token,"
                    "generation_claim_expires_at=now()+interval '150 seconds' "
                    "WHERE id=:message_id"
                ),
                {"token": token, "message_id": message.id},
            )
    with database_engine.begin() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT public.sou2ai_undo_owner_chat_generation_admission("
                    ":business_id,:message_id,1,:claim)"
                ),
                {
                    "business_id": business_id,
                    "message_id": messages[1].id,
                    "claim": tokens[0],
                },
            )
            is False
        )
        connection.execute(
            text(
                "SELECT * FROM public.sou2ai_reserve_ai_usage("
                ":business_id,:user_id,:message_id,1,'owner','owner_chat',"
                "10,10,150)"
            ),
            {
                "business_id": business_id,
                "user_id": user_id,
                "message_id": messages[1].id,
            },
        ).one()
        assert (
            connection.scalar(
                text(
                    "SELECT public.sou2ai_undo_owner_chat_generation_admission("
                    ":business_id,:message_id,1,:claim)"
                ),
                {
                    "business_id": business_id,
                    "message_id": messages[1].id,
                    "claim": tokens[1],
                },
            )
            is False
        )
        assert (
            connection.scalar(
                text(
                    "SELECT public.sou2ai_undo_owner_chat_generation_admission("
                    ":business_id,:message_id,1,:claim)"
                ),
                {
                    "business_id": business_id,
                    "message_id": messages[0].id,
                    "claim": tokens[0],
                },
            )
            is True
        )
    with migration_engine.connect() as connection:
        remaining = (
            connection.execute(
                text("SELECT owner_message_id FROM owner_chat_rate_limit_events")
            )
            .scalars()
            .all()
        )
    assert remaining == [messages[1].id]


def test_rate_admission_sql_injection_argument_is_inert(
    database_engine: Engine, migration_engine: Engine
) -> None:
    payload = "'; DROP TABLE businesses; --"
    with database_engine.begin() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT * FROM public.sou2ai_admit_registration_attempt("
                    ":email,:client_ip)"
                ),
                {"email": payload, "client_ip": "203.0.113.7"},
            )
            .one()
            .admitted
        )
    with migration_engine.connect() as connection:
        assert connection.scalar(text("SELECT to_regclass('public.businesses')"))
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM registration_rate_limit_events "
                    "WHERE normalized_email=:payload"
                ),
                {"payload": payload},
            )
            == 1
        )
