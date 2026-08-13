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
