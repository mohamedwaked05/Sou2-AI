import copy
import importlib.util
import os
import sys
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

sys.path.insert(0, "/")
import activation_guard as g

spec = importlib.util.spec_from_file_location(
    "native_private_runtime", "/private_runtime.py"
)
rt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rt)
BASE = "postgresql+psycopg://sou2ai@127.0.0.1:5433/"


def denied(engine, sql):
    with pytest.raises(DBAPIError) as exc:
        with engine.begin() as c:
            c.execute(text(sql))
    assert exc.value.orig.sqlstate == "42501"
    return exc.value.orig.sqlstate


def rows(engine):
    with engine.connect() as c:
        tables = (
            c.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE "
                    "schemaname='public' ORDER BY "
                    "tablename"
                )
            )
            .scalars()
            .all()
        )
        return {
            n: c.scalar(
                text(
                    (
                        "SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY "
                        "to_jsonb(t)::text),'[]'::jsonb) FROM "
                        'public."'
                    )
                    + n
                    + '" t'
                )
            )
            for n in tables
        }


@pytest.fixture(scope="session")
def db():
    boot = create_engine(BASE + os.environ["PROBE_DB"], hide_parameters=True)
    with boot.connect() as c:
        assert c.execute(text("SELECT current_database(),current_user")).one() == (
            os.environ["PROBE_DB"],
            "sou2ai",
        )
        assert (
            c.scalar(text("SELECT count(*) FROM pg_tables WHERE schemaname='public'"))
            == 0
        )
        assert (
            c.scalar(
                text(
                    "SELECT count(*) FROM pg_database WHERE datname IN "
                    "('sou2ai_dev','sou2ai_test')"
                )
            )
            == 0
        )
    with boot.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text("CREATE DATABASE sou2ai_dev"))
    boot.dispose()
    admin = create_engine(BASE + "sou2ai_dev", hide_parameters=True)
    with admin.begin() as c:
        c.execute(
            text(
                "CREATE ROLE sou2ai_migrator NOLOGIN NOSUPERUSER "
                "NOCREATEDB NOCREATEROLE "
                "NOREPLICATION"
            )
        )
        c.execute(text("GRANT sou2ai_migrator TO sou2ai WITH ADMIN OPTION"))
        c.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
        c.execute(text("GRANT USAGE,CREATE ON SCHEMA public TO sou2ai_migrator"))
    conf = Config()
    conf.set_main_option("script_location", "/app/alembic")
    os.environ["MIGRATION_POSTGRESQL_DATABASE_URL"] = BASE + "sou2ai_dev"
    command.upgrade(conf, "20261007_16")
    with admin.begin() as c:
        for name, base in [
            ("sou2ai_runtime_login", "sou2ai_runtime"),
            ("sou2ai_lifecycle_operator_login", "sou2ai_lifecycle_operator"),
            ("column_unprivileged", None),
        ]:
            c.execute(
                text(
                    "CREATE ROLE "
                    + name
                    + " LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
                )
            )
            if base:
                c.execute(text("GRANT " + base + " TO " + name))
        c.execute(
            text(
                "GRANT USAGE ON SCHEMA public TO "
                "sou2ai_runtime,sou2ai_lifecycle_operator"
            )
        )
        c.execute(text("GRANT ALL ON public.alembic_version TO sou2ai_migrator"))
    os.environ["PGOPTIONS"] = "-c role=sou2ai_migrator"
    try:
        command.upgrade(conf, "20261013_17")
    finally:
        os.environ.pop("PGOPTIONS", None)
    with admin.begin() as c:
        c.execute(text("REVOKE ALL ON public.alembic_version FROM sou2ai_migrator"))
    runtime = create_engine(
        BASE.replace("sou2ai@", "sou2ai_runtime_login@") + "sou2ai_dev",
        hide_parameters=True,
    )
    operator = create_engine(
        BASE.replace("sou2ai@", "sou2ai_lifecycle_operator_login@") + "sou2ai_dev",
        hide_parameters=True,
    )
    other = create_engine(
        BASE.replace("sou2ai@", "column_unprivileged@") + "sou2ai_dev",
        hide_parameters=True,
    )
    before_failure = denied(runtime, "SELECT version_num FROM public.alembic_version")
    with admin.connect() as c:
        before = c.scalar(text(g.CATALOG_SQL))
        assert (
            c.scalar(
                text(
                    "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE "
                    "oid='public.alembic_version'::regclass"
                )
            )
            == "sou2ai"
        )
        assert (
            c.scalar(
                text(
                    "SELECT attacl FROM pg_attribute WHERE "
                    "attrelid='public.alembic_version'::regclass AND "
                    "attname='version_num'"
                )
            )
            is None
        )
    from app.database.models import User
    from sqlalchemy.orm import Session

    with Session(admin) as session:
        session.add(
            User(
                email="synthetic-history@example.invalid",
                first_name="Synthetic",
                last_name="History",
                password_hash="isolated-test-only",
            )
        )
        session.commit()
    before_rows = rows(admin)
    with admin.begin() as c:
        c.execute(
            text(
                "GRANT SELECT(version_num) ON TABLE "
                "public.alembic_version TO "
                "sou2ai_runtime"
            )
        )
    with runtime.connect() as c:
        c.execute(text("SET TRANSACTION READ ONLY"))
        c.execute(text("SET LOCAL statement_timeout='5s'"))
        after = c.scalar(text(g.CATALOG_SQL))
    with admin.connect() as c:
        assert c.scalar(text(g.CATALOG_SQL)) == after
        cluster = c.scalar(
            text("SELECT system_identifier::text FROM pg_control_system()")
        )
    release = {
        "application_source_version": g.application_version("/app/app"),
        "files_sha256": "a" * 64,
        "image_id": os.environ["PROBE_IMAGE"],
        "worker_contract_version": 3,
    }
    # This in-memory fixture receipt is never published as operational evidence.
    receipt = {
        "receipt_version": 2,
        "migration_revision": "20261013_17",
        "prior_revision": "20261007_16",
        "issued_at": datetime.now(UTC).isoformat(),
        "verification_nonce": "b" * 32,
        "release": release,
        "database": {
            "database": "sou2ai_dev",
            "database_oid": after["database_oid"],
            "system_identifier": cluster,
            "container_id": os.environ["PROBE_CONTAINER"],
        },
        "catalog_sha256": g.digest(after),
        "backup": {"dump_bytes": 1, "dump_sha256": "c" * 64, "verified_restore": True},
        "verification": dict.fromkeys(g.CHECKS, True),
    }
    from app.core.config import Settings
    from app.core.runtime import worker_configuration_identity

    receipt["configuration_sha256"] = worker_configuration_identity(
        Settings(_env_file=None)
    )
    yield locals()
    for engine in (admin, runtime, operator, other):
        engine.dispose()


def test_original_42501(db):
    assert db["before_failure"] == "42501"


def test_revision_and_login(db):
    with db["runtime"].connect() as c:
        assert c.execute(
            text("SELECT current_user,version_num FROM public.alembic_version")
        ).one() == ("sou2ai_runtime_login", "20261013_17")


def test_narrow_inherited_column_permission(db):
    with db["admin"].connect() as c:
        for role in ("sou2ai_runtime", "sou2ai_runtime_login"):
            assert c.scalar(
                text(
                    "SELECT has_column_privilege(:r,'public.alembic_version"
                    "','version_num','SELECT')"
                ),
                {"r": role},
            )
            assert not c.scalar(
                text(
                    "SELECT has_table_privilege(:r,'public.alembic_version','SELECT')"
                ),
                {"r": role},
            )


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO public.alembic_version VALUES ('forbidden')",
        "UPDATE public.alembic_version SET version_num='forbidden' WHERE false",
        "DELETE FROM public.alembic_version WHERE false",
        "TRUNCATE public.alembic_version",
        "ALTER TABLE public.alembic_version ADD COLUMN forbidden text",
    ],
)
def test_alembic_writes_denied(db, sql):
    denied(db["runtime"], sql)


@pytest.mark.parametrize("table", ["ai_usage_reservations", "business_ai_usage_daily"])
@pytest.mark.parametrize("action", ["SELECT", "INSERT", "UPDATE", "DELETE"])
def test_accounting_denied(db, table, action):
    col = "status" if table == "ai_usage_reservations" else "tokens_reserved"
    sql = {
        "SELECT": f"SELECT * FROM public.{table} LIMIT 1",
        "INSERT": f"INSERT INTO public.{table} DEFAULT VALUES",
        "UPDATE": f"UPDATE public.{table} SET {col}={col} WHERE false",
        "DELETE": f"DELETE FROM public.{table} WHERE false",
    }[action]
    denied(db["runtime"], sql)


@pytest.mark.parametrize("role", ["operator", "other"])
def test_no_grant_to_other_roles(db, role):
    denied(db[role], "SELECT version_num FROM public.alembic_version")


def test_no_grant_option(db):
    with db["admin"].connect() as c:
        assert not c.scalar(
            text(
                "SELECT has_column_privilege('sou2ai_runtime_login','pu"
                "blic.alembic_version','version_num','SELECT WITH "
                "GRANT OPTION')"
            )
        )


def test_no_escalation(db):
    denied(db["runtime"], "SET ROLE sou2ai_migrator")


def test_exact_catalog_security_checks(db):
    g.validate_catalog(
        db["after"],
        g.expected_functions(
            "/app/alembic/versions/20261013_17_reservation_runtime_guards.py"
        ),
    )
    assert all(
        not x["allowed"]
        for k in ("accounting_permissions", "accounting_columns")
        for x in db["after"][k]
    )


def test_actual_runtime_preflight_with_postgrant_fixture_receipt(db):
    rt.runtime_preflight(
        {
            "activation": db["receipt"],
            "environment": {
                "POSTGRESQL_DATABASE_URL": (
                    "postgresql+psycopg://sou2ai_runtime_login@host.docker."
                    "internal:5433/sou2ai_dev"
                )
            },
        }
    )


def test_old_receipt_fails_closed(db):
    old = copy.deepcopy(db["receipt"])
    old["catalog_sha256"] = g.digest(db["before"])
    with pytest.raises(g.ActivationRejected, match="receipt_catalog_mismatch"):
        rt.runtime_preflight(
            {
                "activation": old,
                "environment": {
                    "POSTGRESQL_DATABASE_URL": (
                        "postgresql+psycopg://sou2ai_runtime_login@host.docker."
                        "internal:5433/sou2ai_dev"
                    )
                },
            }
        )


def test_only_approved_acl_delta(db):
    a, b = db["before"], db["after"]
    assert rows(db["admin"]) == db["before_rows"]
    assert (
        a["existing_functions"] == b["existing_functions"]
        and a["table_acls"] == b["table_acls"]
    )
    assert all(x["object_name"] != "alembic_version" for x in a["column_acls"])
    expected = a["column_acls"] + [
        {
            "object_name": "alembic_version",
            "column_name": "version_num",
            "acl": "{sou2ai_runtime=r/sou2ai}",
        }
    ]
    assert b["column_acls"] == sorted(
        expected, key=lambda x: (x["object_name"], x["column_name"])
    )
    assert {
        k: v
        for k, v in a.items()
        if k not in {"column_acls", "revision_column_acl", "revision_permissions"}
    } == {
        k: v
        for k, v in b.items()
        if k not in {"column_acls", "revision_column_acl", "revision_permissions"}
    }


@pytest.mark.parametrize(
    "sql",
    [
        "REVOKE SELECT(version_num) ON public.alembic_version FROM sou2ai_runtime",
        "GRANT SELECT ON public.alembic_version TO sou2ai_runtime",
        "GRANT INSERT ON public.alembic_version TO sou2ai_runtime",
        "GRANT UPDATE(version_num) ON public.alembic_version TO sou2ai_runtime",
        (
            "GRANT SELECT(version_num) ON public.alembic_version "
            "TO sou2ai_runtime WITH GRANT "
            "OPTION"
        ),
        "GRANT SELECT(version_num) ON public.alembic_version TO sou2ai_runtime_login",
        "GRANT SELECT(version_num) ON public.alembic_version TO PUBLIC",
        "GRANT CREATE ON SCHEMA public TO sou2ai_runtime",
        "GRANT SELECT ON public.ai_usage_reservations TO sou2ai_runtime",
        (
            "GRANT SELECT(tokens_reserved) ON "
            "public.business_ai_usage_daily TO "
            "sou2ai_runtime"
        ),
        (
            "ALTER FUNCTION public.sou2ai_summary_attempt_has_usage"
            "(uuid,uuid,uuid,integer) OWNER TO "
            "sou2ai"
        ),
        (
            "ALTER FUNCTION public.sou2ai_summary_attempt_has_usage"
            "(uuid,uuid,uuid,integer) SECURITY "
            "INVOKER"
        ),
        (
            "ALTER FUNCTION public.sou2ai_summary_attempt_has_usage"
            "(uuid,uuid,uuid,integer) SET "
            "search_path=public"
        ),
        (
            "GRANT EXECUTE ON FUNCTION "
            "public.sou2ai_summary_attempt_has_usage(uuid,uuid,uuid"
            ",integer) TO PUBLIC"
        ),
        "UPDATE public.alembic_version SET version_num='20261007_16'",
        "ALTER ROLE sou2ai_runtime_login NOINHERIT",
    ],
)
def test_native_unsafe_security_changes_rejected(db, sql):
    with db["admin"].connect() as c:
        tx = c.begin()
        try:
            c.execute(text(sql))
            # The administrator can inspect even the deliberately missing-grant case.
            catalog = c.scalar(text(g.CATALOG_SQL))
            with pytest.raises(g.ActivationRejected):
                g.validate_catalog(
                    catalog,
                    g.expected_functions(
                        "/app/alembic/versions/20261013_17_reservation_runtime_"
                        "guards.py"
                    ),
                )
        finally:
            tx.rollback()


def test_native_function_body_change_rejected(db):
    definition = next(
        f["definition"]
        for f in db["after"]["existing_functions"]
        if f["name"] == "sou2ai_summary_attempt_has_usage"
    )
    changed = definition.replace("SELECT EXISTS", "SELECT NOT EXISTS")
    assert changed != definition
    with db["admin"].connect() as c:
        tx = c.begin()
        try:
            c.execute(text(changed))
            catalog = c.scalar(text(g.CATALOG_SQL))
            with pytest.raises(
                g.ActivationRejected, match="function_definition_mismatch"
            ):
                g.validate_catalog(
                    catalog,
                    g.expected_functions(
                        "/app/alembic/versions/20261013_17_reservation_runtime_"
                        "guards.py"
                    ),
                )
        finally:
            tx.rollback()


@pytest.mark.parametrize("field", ["issued_at", "release", "configuration_sha256"])
def test_actual_runtime_rejects_stale_or_mismatched_receipt(db, field):
    receipt = copy.deepcopy(db["receipt"])
    receipt[field] = {
        "issued_at": (datetime.now(UTC) - timedelta(hours=25)).isoformat(),
        "release": {},
        "configuration_sha256": "d" * 64,
    }[field]
    with pytest.raises(g.ActivationRejected):
        rt.runtime_preflight(
            {
                "activation": receipt,
                "environment": {
                    "POSTGRESQL_DATABASE_URL": (
                        "postgresql+psycopg://sou2ai_runtime_login@host.docker."
                        "internal:5433/sou2ai_dev"
                    )
                },
            }
        )


def test_security_baseline_exception_is_exact(db):
    before = {"column_grants": db["before"]["column_acls"], "schemas": ["unchanged"]}
    after = g.revision_read_security(before, "sou2ai")
    assert after["column_grants"] == db["after"]["column_acls"]
    assert after["schemas"] == before["schemas"]
    assert rows(db["admin"]) == db["before_rows"]
