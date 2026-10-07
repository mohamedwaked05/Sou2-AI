"""Restricted-role ownership and ACLs for mapping admission."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


def test_mapping_migration_ownership_and_acl(migration_engine, db_session):
    signature = (
        "public.sou2ai_reserve_source_mapping_usage(uuid,uuid,integer,integer,integer)"
    )
    with migration_engine.connect() as connection:
        function = connection.execute(
            text(
                "SELECT p.prosecdef,p.proconfig,r.rolname "
                "FROM pg_catalog.pg_proc p "
                "JOIN pg_catalog.pg_roles r ON r.oid=p.proowner "
                "WHERE p.oid=CAST(:signature AS regprocedure)"
            ),
            {"signature": signature},
        ).one()
        assert function.prosecdef and function.rolname == "sou2ai_migrator"
        assert "search_path=pg_catalog" in function.proconfig
        for role, expected in (
            ("sou2ai_runtime", True),
            ("sou2ai_lifecycle_operator", False),
        ):
            assert (
                connection.execute(
                    text("SELECT has_function_privilege(:role,:signature,'EXECUTE')"),
                    {"role": role, "signature": signature},
                ).scalar_one()
                == expected
            )
        assert not connection.execute(
            text(
                "SELECT has_table_privilege('sou2ai_runtime',"
                "'public.ai_usage_reservations','SELECT')"
            )
        ).scalar_one()
        index = connection.execute(
            text(
                "SELECT indexdef FROM pg_catalog.pg_indexes WHERE schemaname='public' "
                "AND indexname='uq_operational_sources_active_business'"
            )
        ).scalar_one()
        assert "business_id" in index and "adapter_type" not in index
    with pytest.raises(DBAPIError):
        db_session.execute(text("DELETE FROM public.source_mapping_revisions"))
    db_session.rollback()
