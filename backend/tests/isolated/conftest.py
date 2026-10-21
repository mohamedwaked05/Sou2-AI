"""Native reservation tests: never load the destructive parent test fixtures."""

import os
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


@pytest.fixture(scope="session")
def platform():
    nonce = os.environ.get("SOU2AI_DISPOSABLE_RUN", "")
    if not re.fullmatch(r"[0-9a-f]{12}", nonce):
        pytest.fail("Use the verified disposable-container runner.")
    database = f"sou2ai_disposable_{nonce}"
    admin_url = f"postgresql+psycopg://reservation_test_admin@127.0.0.1:5432/{database}"
    admin = create_engine(admin_url, hide_parameters=True)
    with admin.begin() as connection:
        identity = connection.execute(
            text("SELECT current_database(), current_user")
        ).one()
        empty = (
            connection.scalar(
                text("SELECT count(*) FROM pg_tables WHERE schemaname='public'")
            )
            == 0
        )
        forbidden_databases = connection.scalar(
            text(
                "SELECT count(*) FROM pg_database "
                "WHERE datname IN ('sou2ai_dev','sou2ai_test')"
            )
        )
        if (
            identity != (database, "reservation_test_admin")
            or not empty
            or forbidden_databases
        ):
            pytest.fail(
                "Refusing a nonempty or incorrectly identified database server."
            )
        connection.execute(
            text(
                "CREATE ROLE sou2ai_migrator NOLOGIN NOSUPERUSER "
                "NOCREATEDB NOCREATEROLE NOREPLICATION"
            )
        )
        connection.execute(
            text("GRANT sou2ai_migrator TO reservation_test_admin WITH ADMIN OPTION")
        )
        connection.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
        connection.execute(
            text("GRANT USAGE, CREATE ON SCHEMA public TO sou2ai_migrator")
        )
    config = Config()
    config.set_main_option("script_location", str(Path.cwd() / "alembic"))
    os.environ["MIGRATION_POSTGRESQL_DATABASE_URL"] = admin_url
    command.upgrade(config, "20261007_16")
    with admin.begin() as connection:
        for role, membership in (
            ("sou2ai_runtime_login", "sou2ai_runtime"),
            ("sou2ai_lifecycle_operator_login", "sou2ai_lifecycle_operator"),
            ("reservation_test_unprivileged", None),
        ):
            connection.execute(
                text(
                    f"CREATE ROLE {role} LOGIN INHERIT NOSUPERUSER "
                    "NOCREATEDB NOCREATEROLE NOREPLICATION"
                )
            )
            if membership:
                connection.execute(text(f"GRANT {membership} TO {role}"))
        connection.execute(
            text(
                "GRANT USAGE ON SCHEMA public TO sou2ai_runtime, "
                "sou2ai_lifecycle_operator"
            )
        )
        connection.execute(
            text("ALTER TABLE public.alembic_version OWNER TO sou2ai_migrator")
        )
    from tests.isolated.test_reservation_runtime import catalog_snapshot

    before = catalog_snapshot(admin)
    # Baseline bootstrap needs superuser for pgvector; revision 17 runs as migrator.
    os.environ["PGOPTIONS"] = "-c role=sou2ai_migrator"
    try:
        # Dispose bootstrap's pooled connections so PGOPTIONS is applied.
        admin.dispose()
        with admin.connect() as connection:
            assert connection.scalar(text("SELECT current_user")) == "sou2ai_migrator"
        admin.dispose()
        command.upgrade(config, "20261013_17")
    finally:
        os.environ.pop("PGOPTIONS")
    admin.dispose()
    after = catalog_snapshot(admin)
    runtime_url = admin_url.replace("reservation_test_admin@", "sou2ai_runtime_login@")
    runtime = create_engine(
        runtime_url,
        hide_parameters=True,
        connect_args={"options": "-c statement_timeout=8000 -c lock_timeout=6000"},
    )
    operator = create_engine(
        admin_url.replace(
            "reservation_test_admin@", "sou2ai_lifecycle_operator_login@"
        ),
        hide_parameters=True,
    )
    unprivileged = create_engine(
        admin_url.replace("reservation_test_admin@", "reservation_test_unprivileged@"),
        hide_parameters=True,
    )
    yield {
        "admin": admin,
        "runtime": runtime,
        "operator": operator,
        "unprivileged": unprivileged,
        "factory": sessionmaker(runtime, expire_on_commit=False),
        "before": before,
        "after": after,
        "config": config,
        "admin_url": admin_url,
    }
    for engine in (admin, runtime, operator, unprivileged):
        engine.dispose()


@pytest.fixture
def tenant(platform, monkeypatch):
    """Onboard through real runtime HTTP/services and controlled lifecycle operator."""
    import uuid

    from app.agent.owner_chat_provider import get_owner_chat_provider
    from app.core.config import get_settings
    from app.database.session import get_db_session
    from app.main import app
    from app.services import owner_chat
    from fastapi.testclient import TestClient
    from tests.test_owner_chat import CapturingProvider, active_business

    os.environ["TEST_LIFECYCLE_OPERATOR_POSTGRESQL_DATABASE_URL"] = platform[
        "admin_url"
    ].replace("reservation_test_admin@", "sou2ai_lifecycle_operator_login@")
    get_settings.cache_clear()
    monkeypatch.setattr(owner_chat, "_enqueue_summary_safely", lambda *args: None)

    def sessions():
        with platform["factory"]() as session:
            yield session

    app.dependency_overrides[get_db_session] = sessions
    app.dependency_overrides[get_owner_chat_provider] = CapturingProvider
    with platform["factory"]() as session, TestClient(app) as client:
        user, business = active_business(
            client,
            session,
            email=f"{uuid.uuid4().hex}@example.invalid",
            name=f"Test {uuid.uuid4().hex[:12]}",
        )
        yield user, uuid.UUID(business["id"]), client
    app.dependency_overrides.clear()
