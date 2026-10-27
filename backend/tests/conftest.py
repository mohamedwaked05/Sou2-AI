"""PostgreSQL integration fixtures with an explicit test-database safety gate."""

import os
import re
import uuid
from collections.abc import Generator

os.environ.setdefault("ENVIRONMENT", "testing")
os.environ.setdefault("CONVERSATION_SUMMARY_PROVIDER", "mock")
os.environ.setdefault("CUSTOMER_CHAT_PROVIDER", "mock")
os.environ.setdefault("TRUSTED_HOSTS", "localhost,127.0.0.1,testserver")
os.environ.setdefault("GEMINI_API_KEY", "offline-test-key")

import pytest
from alembic import command
from alembic.config import Config
from app.agent.owner_chat_provider import (
    DeterministicMockOwnerChatProvider,
    get_owner_chat_provider,
)
from app.core.config import Settings, get_settings
from app.database.session import ensure_test_database_url, get_db_session
from app.main import app
from app.rag.embeddings import EmbeddingResult
from app.services import owner_chat
from app.services.email import get_email_service
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

TEST_SETTINGS = Settings(_env_file=None)
TEST_DATABASE_URL = TEST_SETTINGS.test_postgresql_database_url
TEST_MIGRATION_DATABASE_URL = os.getenv(
    "TEST_MIGRATION_POSTGRESQL_DATABASE_URL",
    "postgresql+psycopg://sou2ai:sou2ai_local@127.0.0.1:5433/sou2ai_test",
)
TEST_OPERATOR_DATABASE_URL = os.getenv(
    "TEST_LIFECYCLE_OPERATOR_POSTGRESQL_DATABASE_URL",
    "postgresql+psycopg://sou2ai_lifecycle_operator_login:"
    "sou2ai_lifecycle_operator_local@127.0.0.1:5433/sou2ai_test",
)
os.environ["POSTGRESQL_DATABASE_URL"] = TEST_DATABASE_URL
os.environ["MIGRATION_POSTGRESQL_DATABASE_URL"] = TEST_MIGRATION_DATABASE_URL
os.environ["TEST_LIFECYCLE_OPERATOR_POSTGRESQL_DATABASE_URL"] = (
    TEST_OPERATOR_DATABASE_URL
)
get_settings.cache_clear()
ensure_test_database_url(TEST_DATABASE_URL)
ensure_test_database_url(TEST_MIGRATION_DATABASE_URL)
ensure_test_database_url(TEST_OPERATOR_DATABASE_URL)


@pytest.fixture(scope="session")
def alembic_config() -> Config:
    return Config("alembic.ini")


@pytest.fixture(scope="session")
def migration_engine(alembic_config: Config) -> Generator[Engine]:
    ensure_test_database_url(TEST_MIGRATION_DATABASE_URL)
    command.upgrade(alembic_config, "head")
    engine = create_engine(
        TEST_MIGRATION_DATABASE_URL,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": TEST_SETTINGS.postgresql_connect_timeout_seconds,
        },
    )
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def database_engine(migration_engine: Engine) -> Generator[Engine]:
    ensure_test_database_url(TEST_DATABASE_URL)
    engine = create_engine(
        TEST_DATABASE_URL,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": TEST_SETTINGS.postgresql_connect_timeout_seconds,
        },
    )
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def operator_engine(migration_engine: Engine) -> Generator[Engine]:
    ensure_test_database_url(TEST_OPERATOR_DATABASE_URL)
    engine = create_engine(
        TEST_OPERATOR_DATABASE_URL,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": TEST_SETTINGS.postgresql_connect_timeout_seconds,
        },
    )
    yield engine
    engine.dispose()


@pytest.fixture
def legacy_migration_database(migration_engine: Engine, monkeypatch):
    """Keep reversible historical tests away from permanent revision-19 evidence."""
    nonce = os.environ.get("SOU2AI_DISPOSABLE_RUN", "")
    if not re.fullmatch(r"[a-f0-9]{12}", nonce):
        pytest.fail("Legacy migration rehearsals require a verified disposable server.")
    with migration_engine.connect() as connection:
        assert connection.scalar(text("SELECT current_database()")) == "sou2ai_test"
        assert not connection.scalar(
            text("SELECT EXISTS(SELECT FROM pg_database WHERE datname='sou2ai_dev')")
        )
    name = "sou2ai_legacy_" + nonce + "_" + uuid.uuid4().hex[:12]
    admin = create_engine(
        migration_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    with admin.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
        oid = connection.scalar(
            text("SELECT oid FROM pg_database WHERE datname=:name"), {"name": name}
        )
    url = migration_engine.url.set(database=name)
    monkeypatch.setenv(
        "MIGRATION_POSTGRESQL_DATABASE_URL", url.render_as_string(hide_password=False)
    )
    bootstrap = create_engine(url, hide_parameters=True)
    runtime = create_engine(
        make_url(TEST_DATABASE_URL).set(database=name), hide_parameters=True
    )
    config = Config("alembic.ini")
    try:
        with bootstrap.begin() as connection:
            assert connection.scalar(text("SELECT current_database()")) == name
            connection.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
            connection.execute(
                text("GRANT USAGE,CREATE ON SCHEMA public TO sou2ai_migrator")
            )
        command.upgrade(config, "20261015_18")
        yield runtime, bootstrap, config
    finally:
        runtime.dispose()
        bootstrap.dispose()
        with admin.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT oid FROM pg_database WHERE datname=:name"),
                    {"name": name},
                )
                == oid
            )
            connection.exec_driver_sql(f'DROP DATABASE "{name}"')
        admin.dispose()


def _clear_test_database(engine: Engine) -> None:
    ensure_test_database_url(str(engine.url))
    with engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE business_lifecycle_history DISABLE TRIGGER "
                "trg_business_lifecycle_history_no_truncate"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE business_ai_allowance_audit DISABLE TRIGGER "
                "trg_ai_allowance_audit_no_truncate"
            )
        )
        for table, trigger in (
            ("catalogue_quality_reports", "trg_quality_report_no_truncate"),
            ("catalogue_quality_approvals", "trg_quality_approval_no_truncate"),
        ):
            connection.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}"))
        connection.execute(
            text(
                "TRUNCATE authentication_maintenance_tasks, authentication_events, "
                "registration_rate_limit_events, owner_chat_rate_limit_events, "
                "password_reset_tokens, "
                "email_verification_tokens, refresh_sessions, tool_call_logs, "
                "ai_usage_reservations, business_ai_usage_daily, "
                "business_ai_allowance_audit, business_ai_allowance_configs, "
                "customer_generation_rate_events, inbound_webhook_deliveries, "
                "customer_messages, customer_conversations, "
                "messaging_channel_connections, operational_data_sources, "
                "business_lifecycle_history, knowledge_document_chunks, "
                "knowledge_documents, business_knowledge, owner_chat_messages, "
                "owner_conversation_summaries, "
                "owner_conversations, "
                "business_opening_shifts, "
                "business_opening_days, business_memberships, businesses, users CASCADE"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE business_lifecycle_history ENABLE TRIGGER "
                "trg_business_lifecycle_history_no_truncate"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE business_ai_allowance_audit ENABLE TRIGGER "
                "trg_ai_allowance_audit_no_truncate"
            )
        )
        for table, trigger in (
            ("catalogue_quality_reports", "trg_quality_report_no_truncate"),
            ("catalogue_quality_approvals", "trg_quality_approval_no_truncate"),
        ):
            connection.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}"))


@pytest.fixture
def db_session(database_engine: Engine, migration_engine: Engine) -> Generator[Session]:
    _clear_test_database(migration_engine)
    factory = sessionmaker(bind=database_engine, expire_on_commit=False)
    try:
        with factory() as session:
            yield session
            session.rollback()
    finally:
        _clear_test_database(migration_engine)


class MockEmailService:
    def __init__(self) -> None:
        self.verification_messages: list[tuple[str, str]] = []
        self.password_reset_messages: list[tuple[str, str]] = []

    def send_verification_email(self, recipient: str, token: str) -> None:
        self.verification_messages.append((recipient, token))

    def send_password_reset_email(self, recipient: str, token: str) -> None:
        self.password_reset_messages.append((recipient, token))


class MockEmbeddingProvider:
    model = "gemini-embedding-001"

    def embed(
        self, texts: list[str], *, task_type: str = "RETRIEVAL_DOCUMENT"
    ) -> EmbeddingResult:
        vector = (1.0, *([0.0] * 1023))
        return EmbeddingResult(
            vectors=tuple(vector for _ in texts),
            model=self.model,
        )


@pytest.fixture
def email_service() -> MockEmailService:
    return MockEmailService()


@pytest.fixture
def api_client(
    db_session: Session,
    email_service: MockEmailService,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[TestClient]:
    def override_session() -> Generator[Session]:
        yield db_session

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_email_service] = lambda: email_service
    app.dependency_overrides[get_owner_chat_provider] = (
        DeterministicMockOwnerChatProvider
    )
    monkeypatch.setattr(
        owner_chat, "create_embedding_provider", lambda _: MockEmbeddingProvider()
    )
    monkeypatch.setattr(owner_chat, "_enqueue_summary_safely", lambda *_: None)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        yield client
    app.dependency_overrides.clear()
