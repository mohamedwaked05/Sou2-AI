"""Database URL safety, connectivity, and migration lifecycle tests."""

import pytest
from alembic import command
from alembic.config import Config
from app.core.config import Settings
from app.database.session import (
    _reject_privileged_runtime_connection,
    ensure_test_database_url,
)
from sqlalchemy import Engine, inspect, text
from sqlalchemy.engine import make_url


def test_development_and_test_urls_are_separate() -> None:
    settings = Settings(
        postgresql_database_url=(
            "postgresql+psycopg://sou2ai_runtime_login:sou2ai_runtime_local@"
            "127.0.0.1:5433/sou2ai_dev"
        ),
        test_postgresql_database_url=(
            "postgresql+psycopg://sou2ai_runtime_login:sou2ai_runtime_local@"
            "127.0.0.1:5433/sou2ai_test"
        ),
        _env_file=None,
    )
    assert settings.postgresql_database_url != settings.test_postgresql_database_url
    assert settings.test_postgresql_database_url.endswith("/sou2ai_test")


def test_safety_guard_rejects_development_database() -> None:
    with pytest.raises(ValueError, match="isolated"):
        ensure_test_database_url(
            "postgresql+psycopg://sou2ai_runtime_login:sou2ai_runtime_local@"
            "127.0.0.1:5433/sou2ai_dev"
        )


def test_test_database_uses_docker_endpoint(database_engine: Engine) -> None:
    database_url = make_url(database_engine.url)
    assert database_url.host == "127.0.0.1"
    assert database_url.port == 5433
    assert database_url.database == "sou2ai_test"


def test_postgresql_connection_works(database_engine: Engine) -> None:
    with database_engine.connect() as connection:
        assert connection.scalar(text("SELECT 1")) == 1


def test_runtime_connection_guard_rejects_bootstrap_role(
    migration_engine: Engine,
) -> None:
    connection = migration_engine.raw_connection()
    try:
        with pytest.raises(RuntimeError, match="restricted PostgreSQL runtime role"):
            _reject_privileged_runtime_connection(connection.driver_connection, None)
    finally:
        connection.close()


def test_alembic_downgrade_and_upgrade(
    database_engine: Engine, alembic_config: Config
) -> None:
    database_engine.dispose()
    command.downgrade(alembic_config, "base")
    empty_engine = database_engine.execution_options()
    assert "users" not in inspect(empty_engine).get_table_names()
    empty_engine.dispose()

    command.upgrade(alembic_config, "head")
    assert "tool_call_logs" in inspect(database_engine).get_table_names()


def test_milestone_7_upgrade_backfills_existing_business_allowance(
    database_engine: Engine,
    migration_engine: Engine,
    alembic_config: Config,
) -> None:
    database_engine.dispose()
    command.downgrade(alembic_config, "20260813_02")
    business_id = "10000000-0000-0000-0000-000000000007"
    user_id = "20000000-0000-0000-0000-000000000007"
    conversation_id = "30000000-0000-0000-0000-000000000007"
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id,email,first_name,last_name,password_hash) "
                "VALUES (:user_id,'pre-m7@example.com','Pre','Migration','hash')"
            ),
            {"user_id": user_id},
        )
        connection.execute(
            text(
                "INSERT INTO businesses (id,owner_user_id,name,normalized_name) "
                "VALUES (:business_id,:user_id,'Pre M7','pre m7')"
            ),
            {"business_id": business_id, "user_id": user_id},
        )
        connection.execute(
            text(
                "INSERT INTO owner_conversations (id,business_id) "
                "VALUES (:conversation_id,:business_id)"
            ),
            {"conversation_id": conversation_id, "business_id": business_id},
        )
    command.upgrade(alembic_config, "head")
    with migration_engine.connect() as connection:
        config = connection.execute(
            text(
                "SELECT * FROM business_ai_allowance_configs "
                "WHERE business_id=:business_id"
            ),
            {"business_id": business_id},
        ).one()
        assert config.daily_token_allowance == 20_000
        assert config.owner_reserve_percent == 25
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM owner_conversations WHERE id=:conversation_id"
                ),
                {"conversation_id": conversation_id},
            )
            == 1
        )


def test_security_hardening_upgrade_preserves_existing_rate_and_usage_data(
    database_engine: Engine,
    migration_engine: Engine,
    alembic_config: Config,
) -> None:
    database_engine.dispose()
    command.downgrade(alembic_config, "20260813_03")
    business_id = "10000000-0000-0000-0000-000000000008"
    user_id = "20000000-0000-0000-0000-000000000008"
    conversation_id = "30000000-0000-0000-0000-000000000008"
    message_id = "40000000-0000-0000-0000-000000000008"
    summary_id = "50000000-0000-0000-0000-000000000008"
    reservation_id = "60000000-0000-0000-0000-000000000008"
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id,email,first_name,last_name,password_hash) "
                "VALUES (:user_id,'pre-hardening@example.com','Pre','Hardening','hash')"
            ),
            {"user_id": user_id},
        )
        connection.execute(
            text(
                "INSERT INTO businesses (id,owner_user_id,name,normalized_name) "
                "VALUES (:business_id,:user_id,'Pre Hardening','pre hardening')"
            ),
            {"business_id": business_id, "user_id": user_id},
        )
        connection.execute(
            text(
                "INSERT INTO owner_conversations (id,business_id) "
                "VALUES (:conversation_id,:business_id)"
            ),
            {"conversation_id": conversation_id, "business_id": business_id},
        )
        connection.execute(
            text(
                "INSERT INTO owner_chat_messages "
                "(id,conversation_id,sequence_number,role,content,idempotency_key,"
                "generation_state,generation_attempts) VALUES "
                "(:message_id,:conversation_id,1,'owner','pre-hardening message',"
                "'pre-hardening','failed',1)"
            ),
            {"message_id": message_id, "conversation_id": conversation_id},
        )
        connection.execute(
            text(
                "INSERT INTO registration_rate_limit_events "
                "(id,normalized_email,client_ip) VALUES "
                "(gen_random_uuid(),'preserved@example.com','198.51.100.7')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO owner_chat_rate_limit_events "
                "(id,business_id,owner_message_id,generation_attempt) VALUES "
                "(gen_random_uuid(),:business_id,:message_id,1)"
            ),
            {"business_id": business_id, "message_id": message_id},
        )
        connection.execute(
            text(
                "INSERT INTO business_ai_usage_daily "
                "(id,business_id,window_start,window_end,input_tokens_used,"
                "output_tokens_used,total_tokens_used,tokens_reserved) VALUES "
                "(:summary_id,:business_id,'2026-08-13T00:00:00Z',"
                "'2026-08-14T00:00:00Z',4,1,5,0)"
            ),
            {"summary_id": summary_id, "business_id": business_id},
        )
        connection.execute(
            text(
                "INSERT INTO ai_usage_reservations "
                "(id,business_id,user_id,owner_message_id,generation_attempt,"
                "channel,capability,estimated_input_tokens,max_output_tokens,"
                "reserved_tokens,input_tokens,output_tokens,total_tokens,"
                "counts_authoritative,status,window_start,window_end,"
                "lease_expires_at,created_at,reconciled_at) VALUES "
                "(:reservation_id,:business_id,:user_id,:message_id,1,'owner',"
                "'owner_chat',4,1,5,4,1,5,true,'completed',"
                "'2026-08-13T00:00:00Z','2026-08-14T00:00:00Z',"
                "'2026-08-13T00:02:30Z','2026-08-13T00:00:00Z',"
                "'2026-08-13T00:01:00Z')"
            ),
            {
                "reservation_id": reservation_id,
                "business_id": business_id,
                "user_id": user_id,
                "message_id": message_id,
            },
        )
    command.upgrade(alembic_config, "head")
    with migration_engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM registration_rate_limit_events "
                    "WHERE normalized_email='preserved@example.com'"
                )
            )
            == 1
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM owner_chat_rate_limit_events "
                    "WHERE owner_message_id=:message_id"
                ),
                {"message_id": message_id},
            )
            == 1
        )
        assert (
            connection.scalar(
                text(
                    "SELECT total_tokens_used FROM business_ai_usage_daily "
                    "WHERE id=:summary_id"
                ),
                {"summary_id": summary_id},
            )
            == 5
        )
        assert (
            connection.scalar(
                text("SELECT total_tokens FROM ai_usage_reservations WHERE id=:id"),
                {"id": reservation_id},
            )
            == 5
        )
