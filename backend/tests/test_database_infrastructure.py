"""Database URL safety, connectivity, and migration lifecycle tests."""

import pytest
from alembic import command
from alembic.config import Config
from app.core.config import Settings
from app.database.session import ensure_test_database_url
from sqlalchemy import Engine, inspect, text
from sqlalchemy.engine import make_url


def test_development_and_test_urls_are_separate() -> None:
    settings = Settings(
        postgresql_database_url=(
            "postgresql+psycopg://sou2ai:sou2ai_local@127.0.0.1:5433/sou2ai_dev"
        ),
        test_postgresql_database_url=(
            "postgresql+psycopg://sou2ai:sou2ai_local@127.0.0.1:5433/sou2ai_test"
        ),
        _env_file=None,
    )
    assert settings.postgresql_database_url != settings.test_postgresql_database_url
    assert settings.test_postgresql_database_url.endswith("/sou2ai_test")


def test_safety_guard_rejects_development_database() -> None:
    with pytest.raises(ValueError, match="isolated"):
        ensure_test_database_url(
            "postgresql+psycopg://sou2ai:sou2ai_local@127.0.0.1:5433/sou2ai_dev"
        )


def test_test_database_uses_docker_endpoint(database_engine: Engine) -> None:
    database_url = make_url(database_engine.url)
    assert database_url.host == "127.0.0.1"
    assert database_url.port == 5433
    assert database_url.database == "sou2ai_test"


def test_postgresql_connection_works(database_engine: Engine) -> None:
    with database_engine.connect() as connection:
        assert connection.scalar(text("SELECT 1")) == 1


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
