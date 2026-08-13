"""PostgreSQL integration fixtures with an explicit test-database safety gate."""

import os
from collections.abc import Generator

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
from app.services.email import get_email_service
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

TEST_SETTINGS = Settings()
TEST_DATABASE_URL = TEST_SETTINGS.test_postgresql_database_url
os.environ["POSTGRESQL_DATABASE_URL"] = TEST_DATABASE_URL
get_settings.cache_clear()
ensure_test_database_url(TEST_DATABASE_URL)


@pytest.fixture(scope="session")
def alembic_config() -> Config:
    return Config("alembic.ini")


@pytest.fixture(scope="session")
def database_engine(alembic_config: Config) -> Generator[Engine]:
    ensure_test_database_url(TEST_DATABASE_URL)
    command.upgrade(alembic_config, "head")
    engine = create_engine(
        TEST_DATABASE_URL,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": TEST_SETTINGS.postgresql_connect_timeout_seconds,
        },
    )
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(database_engine: Engine) -> Generator[Session]:
    factory = sessionmaker(bind=database_engine, expire_on_commit=False)
    with factory() as session:
        yield session
        session.rollback()
    with database_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE business_lifecycle_history DISABLE TRIGGER "
                "trg_business_lifecycle_history_no_truncate"
            )
        )
        connection.execute(
            text(
                "TRUNCATE authentication_maintenance_tasks, authentication_events, "
                "password_reset_tokens, "
                "email_verification_tokens, refresh_sessions, tool_call_logs, "
                "business_lifecycle_history, business_knowledge, owner_chat_messages, "
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


class MockEmailService:
    def __init__(self) -> None:
        self.verification_messages: list[tuple[str, str]] = []
        self.password_reset_messages: list[tuple[str, str]] = []

    def send_verification_email(self, recipient: str, token: str) -> None:
        self.verification_messages.append((recipient, token))

    def send_password_reset_email(self, recipient: str, token: str) -> None:
        self.password_reset_messages.append((recipient, token))


@pytest.fixture
def email_service() -> MockEmailService:
    return MockEmailService()


@pytest.fixture
def api_client(
    db_session: Session, email_service: MockEmailService
) -> Generator[TestClient]:
    def override_session() -> Generator[Session]:
        yield db_session

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_email_service] = lambda: email_service
    app.dependency_overrides[get_owner_chat_provider] = (
        DeterministicMockOwnerChatProvider
    )
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        yield client
    app.dependency_overrides.clear()
