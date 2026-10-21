"""Explicitly staged synthetic dump/restore and transactional migration recovery.

Run only through run-native-validation.ps1 -MigrationRestore. No live backup is loaded.
"""

import copy
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError


def capture(engine):
    """Small synthetic rows plus named ACL/definition evidence; no role OIDs."""
    with engine.connect() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        connection.execute(text("SET LOCAL statement_timeout='5s'"))
        tables = connection.scalars(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                "AND tablename NOT IN ('alembic_version',"
                "'source_operational_mapping_revisions') "
                "ORDER BY tablename"
            )
        ).all()
        rows = {}
        for table in tables:
            assert re.fullmatch(r"[a-z_]+", table)
            rows[table] = connection.scalar(
                text(
                    "SELECT coalesce(jsonb_agg(to_jsonb(t) "
                    "ORDER BY to_jsonb(t)::text),'[]') "
                    f'FROM public."{table}" t'
                )
            )
        functions = connection.scalar(
            text("""SELECT jsonb_agg(to_jsonb(t) ORDER BY name,args)
            FROM (SELECT p.proname name,pg_get_function_identity_arguments(p.oid) args,
              pg_get_userbyid(p.proowner) owner,p.proacl::text acl,p.prosecdef definer,
              p.proconfig config,pg_get_functiondef(p.oid) definition
              FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
              WHERE n.nspname='public' AND p.prokind='f'
              AND p.proname <> 'sou2ai_guard_operational_mapping') t""")
        )
        acls = connection.scalar(
            text("""SELECT jsonb_agg(to_jsonb(t) ORDER BY name)
            FROM (SELECT c.relname name,pg_get_userbyid(c.relowner) owner,
              (SELECT jsonb_agg(jsonb_build_object(
                 'grantee',CASE WHEN x.grantee=0 THEN 'PUBLIC'
                     ELSE pg_get_userbyid(x.grantee) END,
                 'grantor',pg_get_userbyid(x.grantor),
                 'privilege',x.privilege_type,'grantable',x.is_grantable)
                 ORDER BY x.grantee,x.grantor,x.privilege_type)
               FROM aclexplode(coalesce(c.relacl,acldefault(
                 CASE WHEN c.relkind='S' THEN 's'::char
                      ELSE 'r'::char END::"char",c.relowner))) x) acl
              FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
              WHERE n.nspname='public' AND c.relkind IN ('r','S')
              AND c.relname <> 'source_operational_mapping_revisions') t""")
        )
        columns = connection.scalar(
            text("""SELECT jsonb_agg(to_jsonb(t) ORDER BY name,column_name)
            FROM (SELECT c.relname name,a.attname column_name,
              pg_catalog.format_type(a.atttypid,a.atttypmod) kind,
              a.attnotnull required,a.attacl::text acl
              FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
              WHERE c.relnamespace='public'::regnamespace AND c.relkind='r'
              AND a.attnum>0 AND NOT a.attisdropped
              AND c.relname <> 'source_operational_mapping_revisions') t""")
        )
        roles = connection.scalar(
            text("""SELECT jsonb_agg(to_jsonb(t) ORDER BY name)
            FROM (SELECT rolname name,rolsuper,rolinherit,rolcreaterole,rolcreatedb,
              rolcanlogin,rolreplication,rolbypassrls FROM pg_roles
              WHERE rolname NOT LIKE 'pg_%') t""")
        )
        memberships = connection.scalar(
            text("""SELECT jsonb_agg(to_jsonb(t) ORDER BY role,member)
            FROM (SELECT pg_get_userbyid(roleid) role,pg_get_userbyid(member) member,
              admin_option,inherit_option,set_option FROM pg_auth_members) t""")
        )
        constraints = connection.scalar(
            text("""SELECT jsonb_agg(to_jsonb(t) ORDER BY name,definition)
          FROM (SELECT conname name,conrelid::regclass::text table_name,
            contype kind,convalidated validated,pg_get_constraintdef(oid) definition
          FROM pg_constraint WHERE connamespace='public'::regnamespace
          AND conrelid > 0 AND conrelid <> coalesce(
            to_regclass('public.source_operational_mapping_revisions'),0)) t""")
        )
        indexes = connection.scalar(
            text("""SELECT jsonb_agg(to_jsonb(t) ORDER BY indexname)
          FROM (SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='public'
          AND tablename <> 'source_operational_mapping_revisions') t""")
        )
        triggers = connection.scalar(
            text("""SELECT jsonb_agg(to_jsonb(t) ORDER BY name)
          FROM (SELECT tgname name,tgenabled enabled,pg_get_triggerdef(oid) definition
          FROM pg_trigger WHERE NOT tgisinternal
          AND tgname <> 'trg_operational_mapping_immutable'
          AND tgrelid IN (SELECT oid FROM pg_class
            WHERE relnamespace='public'::regnamespace)) t""")
        )
        return dict(
            rows=rows,
            functions=functions,
            acls=acls,
            columns=columns,
            roles=roles,
            memberships=memberships,
            constraints=constraints,
            indexes=indexes,
            triggers=triggers,
        )


def assert_preserved(engine, before):
    # Match the existing reviewed backup_verification.compare_restore rule:
    # PostgreSQL may reparse CHECK cast trees during restore. Accept only its
    # own exact reparse result, never regex-based removal of casts/expressions.
    expected, actual = copy.deepcopy(before), capture(engine)
    targets = {(r["table_name"], r["name"]): r for r in actual["constraints"]}
    for old in expected["constraints"]:
        new = targets.get((old["table_name"], old["name"]))
        assert new is not None
        if old != new:
            assert old["kind"] == "c" and old["definition"].startswith("CHECK ")
            assert old["validated"] == new["validated"]
            table = old["table_name"]
            assert re.fullmatch(r"[a-z_]+", table)
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    connection.execute(
                        text(
                            "CREATE TEMP TABLE restore_canonical "
                            f'(LIKE public."{table}")'
                        )
                    )
                    connection.execute(
                        text(
                            "ALTER TABLE restore_canonical ADD CONSTRAINT canonical "
                            + old["definition"]
                        )
                    )
                    canonical = connection.scalar(
                        text(
                            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                            "WHERE conrelid='pg_temp.restore_canonical'::regclass "
                            "AND conname='canonical'"
                        )
                    )
                finally:
                    transaction.rollback()
            assert canonical == new["definition"]
            old["definition"] = canonical
    assert expected == actual


def test_seed_revision17(platform, tenant):
    from app.database.models import (
        OperationalDataSourceConfig,
        OwnerConversationSummary,
        SourceMappingRevision,
    )
    from app.services.ai_usage import reconcile_ai_usage
    from tests.isolated.test_reservation_runtime import owner_claim, seed_turns
    from tests.offline.test_mapped_operations import fixture_source

    assert os.environ.get("SOU2AI_MIGRATION_STAGE") == "seed"
    # A real controlled reservation/uncertain charge, not fabricated arithmetic.
    *_, reservation = owner_claim(platform, tenant)
    with platform["factory"]() as session:
        reconcile_ai_usage(
            session,
            reservation.id,
            usage=None,
            outcome="uncertain",
        )
        session.commit()
    for _ in range(2):
        conversation = seed_turns(platform, tenant)
        with platform["factory"]() as session:
            session.add(
                OwnerConversationSummary(
                    business_id=tenant[1],
                    conversation_id=conversation,
                    content="Synthetic legacy summary preserved verbatim",
                    summary_version=1,
                    summarized_through_sequence_number=2,
                    last_charged_through_sequence_number=2,
                    provider_identifier="mock",
                    model_identifier="deterministic-v1",
                )
            )
            session.commit()
    products = fixture_source(987)
    try:
        with platform["factory"]() as session:
            source = OperationalDataSourceConfig(
                id=uuid.uuid4(),
                business_id=tenant[1],
                display_name="Synthetic catalogue",
                adapter_type="postgresql_readonly",
                connection_profile_key="fixture_restore",
                mapping_profile_key="discovered_products",
                mapping_profile_version=1,
                status="CONFIGURED",
            )
            session.add(source)
            session.flush()
            revision = SourceMappingRevision(
                id=uuid.uuid4(),
                business_id=tenant[1],
                source_id=source.id,
                requested_by=tenant[0].id,
                idempotency_key=uuid.uuid4(),
                version=1,
                status="proposing",
                discovery=products.discovery.model_dump(mode="json"),
                schema_fingerprint=products.discovery.schema_fingerprint,
            )
            session.add(revision)
            session.commit()
            revision.status = "review"
            session.commit()
            revision.status = "approved"
            revision.approved_mapping = products.catalogue.mapping.model_dump(
                mode="json"
            )
            revision.approved_by = tenant[0].id
            revision.approved_at = datetime.now(UTC)
            session.commit()
    finally:
        products.engine.dispose()
    with platform["admin"].begin() as connection:
        connection.execute(
            text(
                "GRANT SELECT(version_num) ON public.alembic_version TO sou2ai_runtime"
            )
        )
        connection.execute(text("CREATE SCHEMA fixture_evidence"))
        connection.execute(
            text("CREATE TABLE fixture_evidence.baseline(value jsonb NOT NULL)")
        )
    snapshot = capture(platform["admin"])
    assert len(snapshot["rows"]) == 34
    with platform["admin"].begin() as connection:
        connection.execute(
            text(
                "INSERT INTO fixture_evidence.baseline VALUES (CAST(:value AS jsonb))"
            ),
            {"value": json.dumps(snapshot)},
        )


@pytest.fixture
def restored(monkeypatch):
    nonce = os.environ.get("SOU2AI_DISPOSABLE_RUN", "")
    assert re.fullmatch(r"[0-9a-f]{12}", nonce)
    assert os.environ.get("SOU2AI_MIGRATION_STAGE") == "restored"
    name = f"sou2ai_restored_{nonce}"
    url = f"postgresql+psycopg://reservation_test_admin@127.0.0.1:5432/{name}"
    engine = create_engine(url, hide_parameters=True)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT current_database(),current_user")
        ).one() == (name, "reservation_test_admin")
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM pg_database "
                    "WHERE datname IN ('sou2ai_dev','sou2ai_test')"
                )
            )
            == 0
        )
        baseline = connection.scalar(
            text("SELECT value FROM fixture_evidence.baseline")
        )
    monkeypatch.setenv("MIGRATION_POSTGRESQL_DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(Path.cwd() / "alembic"))
    yield engine, baseline, config, url
    engine.dispose()


def test_restored_revision17_data_and_security(restored):
    engine, baseline, _, _ = restored
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM public.alembic_version"))
            == "20261013_17"
        )
    assert_preserved(engine, baseline)


def test_failed_upgrade_rolls_back_then_owner_retry_succeeds(restored, monkeypatch):
    engine, baseline, config, _ = restored
    # Actual migration path rejects missing REFERENCES, preserving revision 17.
    monkeypatch.setenv("PGOPTIONS", "-c role=sou2ai_migrator")
    with pytest.raises(DBAPIError) as denied:
        command.upgrade(config, "20261015_18")
    assert denied.value.orig.sqlstate == "42501"
    monkeypatch.delenv("PGOPTIONS")
    assert_preserved(engine, baseline)
    execute = Operations.execute

    def injected_failure(self, sql, *args, **kwargs):
        execute(self, sql, *args, **kwargs)
        raise RuntimeError("synthetic_failure_after_migration18_ddl")

    with monkeypatch.context() as fault:
        fault.setattr(Operations, "execute", injected_failure)
        with pytest.raises(RuntimeError, match="synthetic_failure_after"):
            command.upgrade(config, "20261015_18")
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM public.alembic_version"))
            == "20261013_17"
        )
        assert (
            connection.scalar(
                text(
                    "SELECT to_regclass('public.source_operational_mapping_revisions')"
                )
            )
            is None
        )
        assert (
            connection.scalar(
                text(
                    "SELECT to_regprocedure("
                    "'public.sou2ai_guard_operational_mapping()')"
                )
            )
            is None
        )
    assert_preserved(engine, baseline)
    command.upgrade(config, "20261015_18")


def test_revision18_preserves_restored_history_and_restricted_security(restored):
    engine, baseline, _, url = restored
    assert_preserved(engine, baseline)
    source = create_engine(
        url.replace("sou2ai_restored_", "sou2ai_disposable_"), hide_parameters=True
    )
    try:
        assert_preserved(source, baseline)
    finally:
        source.dispose()
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM public.alembic_version"))
            == "20261015_18"
        )
        assert connection.execute(
            text("""SELECT pg_get_userbyid(proowner),prosecdef,proconfig
            FROM pg_proc
            WHERE oid='public.sou2ai_guard_operational_mapping()'::regprocedure""")
        ).one() == ("sou2ai_migrator", False, ["search_path=pg_catalog"])
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.source_operational_mapping_revisions")
            )
            == 0
        )
    runtime = create_engine(
        url.replace("reservation_test_admin@", "sou2ai_runtime_login@"),
        hide_parameters=True,
    )
    try:
        with runtime.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == "20261015_18"
            )
            for table in ("ai_usage_reservations", "business_ai_usage_daily"):
                for privilege in ("SELECT", "INSERT", "UPDATE"):
                    assert not connection.scalar(
                        text(
                            "SELECT has_any_column_privilege("
                            "current_user,:table,:privilege)"
                        ),
                        {"table": "public." + table, "privilege": privilege},
                    )
                assert not connection.scalar(
                    text("SELECT has_table_privilege(current_user,:table,'DELETE')"),
                    {"table": "public." + table},
                )
    finally:
        runtime.dispose()
