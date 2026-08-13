"""Alembic environment using application settings and complete model metadata."""

from logging.config import fileConfig

from alembic import context
from app.database import models  # noqa: F401
from app.database.base import Base
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import engine_from_config, pool


class MigrationSettings(BaseSettings):
    """Bootstrap-only settings that are never loaded by the FastAPI runtime."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    migration_postgresql_database_url: str = (
        "postgresql+psycopg://sou2ai:sou2ai_local@127.0.0.1:5433/sou2ai_dev"
    )
    postgresql_connect_timeout_seconds: int = 5


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = MigrationSettings()
config.set_main_option(
    "sqlalchemy.url",
    settings.migration_postgresql_database_url.replace("%", "%%"),
)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"connect_timeout": settings.postgresql_connect_timeout_seconds},
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
