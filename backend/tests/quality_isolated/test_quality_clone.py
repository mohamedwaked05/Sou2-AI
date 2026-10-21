"""Rehearse revision 19 on a restored, disposable revision-18 database."""

import os
import re
import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from app.database.models import OperationalDataSourceConfig, SourceMappingRevision
from sqlalchemy import create_engine, text
from tests.test_source_mapping import opaque_discovery, opaque_mapping


def test_seed_revision18(platform, tenant):
    user, business_id, _ = tenant
    command.upgrade(platform["config"], "20261015_18")
    with platform["factory"]() as session:
        source = OperationalDataSourceConfig(
            business_id=business_id,
            display_name="Preserved source",
            adapter_type="postgresql_readonly",
            connection_profile_key="clone_fixture",
            mapping_profile_key="discovered_products",
            mapping_profile_version=1,
            status="CONFIGURED",
        )
        session.add(source)
        session.flush()
        for version, status in enumerate(("approved", "failed", "review"), 1):
            revision = SourceMappingRevision(
                business_id=business_id,
                source_id=source.id,
                requested_by=user.id,
                idempotency_key=uuid.uuid4(),
                version=version,
                status="proposing",
                discovery=opaque_discovery().model_dump(mode="json"),
                schema_fingerprint=opaque_discovery().schema_fingerprint,
                validation_notes=[],
            )
            session.add(revision)
            session.flush()
            revision.status = "review" if status == "approved" else status
            revision.proposal = {
                "mapping": opaque_mapping().model_dump(mode="json"),
                "rationale": "Preserved fixture",
                "uncertainties": [],
            }
            session.flush()
            if status == "approved":
                from datetime import UTC, datetime

                revision.status = "approved"
                revision.approved_mapping = opaque_mapping().model_dump(mode="json")
                revision.approved_by = user.id
                revision.approved_at = datetime.now(UTC)
        session.commit()


def rows(connection, tables=None):
    if tables is None:
        tables = connection.scalars(
            text(
                "SELECT tablename FROM pg_tables WHERE "
                "schemaname='public' AND tablename <> 'alembic_version' "
                "ORDER BY tablename"
            )
        ).all()
    quote = connection.dialect.identifier_preparer.quote_identifier
    return {
        name: tuple(
            connection.execute(
                text(
                    "SELECT count(*), md5(COALESCE(string_agg("
                    "md5(row_to_json(t)::text),'' ORDER BY "
                    "md5(row_to_json(t)::text)),'')) "
                    f"FROM public.{quote(name)} t"
                )
            ).one()
        )
        for name in tables
    }


def test_clone_upgrade_preserves_all_existing_rows_and_security():
    nonce = os.environ.get("SOU2AI_DISPOSABLE_RUN", "")
    assert re.fullmatch(r"[a-f0-9]{12}", nonce)
    original = create_engine(
        f"postgresql+psycopg://reservation_test_admin@127.0.0.1:5432/sou2ai_disposable_{nonce}"
    )
    clone = create_engine(
        f"postgresql+psycopg://reservation_test_admin@127.0.0.1:5432/sou2ai_restored_{nonce}"
    )
    with original.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "20261015_18"
        )
        baseline = rows(connection)
        assert (
            connection.scalar(text("SELECT count(*) FROM source_mapping_revisions"))
            == 3
        )
        privileges = connection.execute(
            text(
                "SELECT table_name,grantee,privilege_type FROM "
                "information_schema.role_table_grants WHERE "
                "table_schema='public' ORDER BY 1,2,3"
            )
        ).all()
    with clone.begin() as connection:
        assert (
            connection.scalar(text("SELECT current_database()"))
            == f"sou2ai_restored_{nonce}"
        )
        assert rows(connection) == baseline
        config = Config()
        config.set_main_option("script_location", str(Path.cwd() / "alembic"))
        config.attributes["connection"] = connection
        command.upgrade(config, "20261018_19")
        assert rows(connection, baseline) == baseline
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "20261018_19"
        )
        assert (
            connection.execute(
                text(
                    "SELECT table_name,grantee,privilege_type FROM "
                    "information_schema.role_table_grants WHERE "
                    "table_schema='public' AND table_name NOT IN "
                    "('catalogue_quality_reports','catalogue_quality_approvals')"
                    " ORDER BY 1,2,3"
                )
            ).all()
            == privileges
        )
        assert (
            connection.scalar(text("SELECT count(*) FROM catalogue_quality_reports"))
            == 0
        )
        assert (
            connection.scalar(text("SELECT count(*) FROM catalogue_quality_approvals"))
            == 0
        )
    original.dispose()
    clone.dispose()
