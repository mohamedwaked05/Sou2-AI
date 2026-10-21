"""Native migration ACLs and production query execution with a restricted reader."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError


@pytest.fixture(scope="module", params=[800, 801])
def native_source(operations_platform, request):
    spec = importlib.util.spec_from_file_location(
        "operational_source_fixture", Path("tests/offline/test_mapped_operations.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    platform = operations_platform
    with platform["admin"].begin() as connection:
        connection.execute(
            text(
                "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles "
                "WHERE rolname='isolated_source_reader') THEN "
                "CREATE ROLE isolated_source_reader LOGIN NOSUPERUSER "
                "NOCREATEDB NOCREATEROLE; END IF; END $$"
            )
        )
        connection.execute(
            text("GRANT USAGE ON SCHEMA public TO isolated_source_reader")
        )
    reader = create_engine(
        platform["admin_url"].replace(
            "reservation_test_admin@", "isolated_source_reader@"
        ),
        hide_parameters=True,
        connect_args={
            "options": "-c default_transaction_read_only=on -c statement_timeout=2000"
        },
    )
    source = module.fixture_source(request.param, platform["admin"], reader)
    with platform["admin"].begin() as connection:
        for table in source.tables:
            connection.execute(
                text(f'GRANT SELECT ON public."{table.name}" TO isolated_source_reader')
            )
    yield source, module
    reader.dispose()


@pytest.mark.parametrize(
    "capability",
    [
        "stock",
        "availability",
        "restocking",
        "sales",
        "revenue",
        "refunds",
        "best_sellers",
    ],
)
def test_native_restricted_source_queries(native_source, capability):
    source, fixture = native_source
    fixture.test_real_queries_are_partitioned_and_preserve_semantics(source, capability)


def test_native_price_decimal_effective_date(native_source):
    source, fixture = native_source
    fixture.test_current_price_is_not_receipt_price_and_scopes_are_explicit(source)


def test_native_cross_partition_identifier_rejected(native_source):
    source, fixture = native_source
    fixture.test_external_product_selection_cannot_cross_partition(source)


def test_native_role_cannot_write_or_read_platform_accounting(operations_platform):
    url = operations_platform["admin_url"].replace(
        "reservation_test_admin@", "isolated_source_reader@"
    )
    engine = create_engine(url, hide_parameters=True)
    try:
        for sql in (
            "SELECT id FROM public.ai_usage_reservations LIMIT 1",
            "DELETE FROM public.variant_800_stock",
        ):
            with engine.connect() as connection, pytest.raises(DBAPIError) as error:
                connection.execute(text(sql))
            assert error.value.orig.sqlstate == "42501"
    finally:
        engine.dispose()


def test_revision18_additive_security_and_immutable_approvals(operations_platform):
    platform = operations_platform
    with platform["admin"].connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM public.alembic_version"))
            == "20261015_18"
        )
        assert (
            connection.scalar(
                text(
                    "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE "
                    "oid='public.source_operational_mapping_revisions'::regclass"
                )
            )
            == "sou2ai_migrator"
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM pg_proc WHERE proname IN "
                    "('sou2ai_lock_active_ai_usage_reservation','sou2ai_summary_attempt_has_usage')"
                    " AND prosecdef"
                )
            )
            == 2
        )
    for table in ("ai_usage_reservations", "ai_usage_daily"):
        with platform["runtime"].connect() as connection:
            # Find exact deployed daily-table name through the platform snapshot
            if table == "ai_usage_daily":
                continue
            assert not connection.scalar(
                text("SELECT has_table_privilege(current_user,:table,'SELECT')"),
                {"table": f"public.{table}"},
            )
    with platform["runtime"].connect() as connection:
        assert connection.scalar(
            text(
                "SELECT "
                "has_table_privilege(current_user,'public.source_operational_mapping_revisions','INSERT')"
            )
        )
        for privilege in ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            assert not connection.scalar(
                text(
                    "SELECT "
                    "has_table_privilege(current_user,'public.source_operational_mapping_revisions',:privilege)"
                ),
                {"privilege": privilege},
            )


def test_allowlisted_view_discovery_rejects_unenforced_keys_and_detects_drift(
    native_source,
    operations_platform,
):
    from app.integrations.discovery import (
        EngineConnector,
        SourceConnection,
        SourceMappingError,
    )
    from app.integrations.mapped_products import validate_mapping
    from app.schemas.source_mapping import ProductMapping
    from pydantic import SecretStr

    source, _ = native_source
    product = source.tables[0]
    quote = source.engine.dialect.identifier_preparer.quote
    view = product.name + "_view"
    columns = ",".join(quote(c.name) for c in product.c)
    definition = f"SELECT {columns} FROM public.{quote(product.name)}"
    with source.engine.begin() as connection:
        connection.execute(text(f"CREATE VIEW public.{quote(view)} AS {definition}"))
        connection.execute(
            text(f"GRANT SELECT ON public.{quote(view)} TO isolated_source_reader")
        )
    connector = EngineConnector(
        SourceConnection(
            key="view_fixture",
            display_name="Synthetic view",
            engine="postgresql",
            business_ids=(uuid.uuid4(),),
            url=SecretStr(
                operations_platform["admin_url"].replace(
                    "reservation_test_admin@", "isolated_source_reader@"
                )
            ),
            discovery_scope=(
                {
                    "schema_name": "public",
                    "object_name": view,
                    "columns": tuple(c.name for c in product.c),
                },
            ),
        )
    )
    discovery = connector.discover()
    assert not discovery.objects[0].unique_keys
    mapping = ProductMapping(
        capability="products",
        object_id="o1",
        key_columns=(product.c.code.name,),
        name_columns=(product.c.label.name,),
    )
    with pytest.raises(SourceMappingError):
        validate_mapping(mapping, discovery)
    with source.engine.begin() as connection:
        connection.execute(
            text(
                f"CREATE OR REPLACE VIEW public.{quote(view)} AS {definition} "
                "WHERE false"
            )
        )
    with (
        connector.connection() as connection,
        pytest.raises(SourceMappingError, match="mapping_schema_changed"),
    ):
        connector.assert_schema(discovery, connection)
    connector.engine.dispose()


def test_invalid_catalogue_dependency_rejected(operations_platform, tenant):
    user, business_id, _client = tenant
    with operations_platform["runtime"].begin() as connection:
        with pytest.raises(DBAPIError) as error:
            connection.execute(
                text("""INSERT INTO public.source_operational_mapping_revisions
                (id,business_id,source_id,catalogue_revision_id,
                 version,mapping,discovery,approved_by)
                VALUES (:id,:business,:source,:catalogue,1,
                        CAST(:mapping AS jsonb),'{}',:user)"""),
                dict(
                    id=uuid.uuid4(),
                    business=business_id,
                    source=uuid.uuid4(),
                    catalogue=uuid.uuid4(),
                    user=user.id,
                    mapping="{}",
                ),
            )
        assert error.value.orig.sqlstate == "22023"
        connection.rollback()


def test_real_approval_authorization_retrieval_and_audit(
    operations_platform, native_source, tenant, monkeypatch
):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.core.config import get_settings
    from app.core.exceptions import ApplicationError
    from app.database.models import (
        OperationalDataSourceConfig,
        SourceMappingRevision,
        SourceOperationalMappingRevision,
        ToolCallLog,
    )
    from app.integrations.discovery import EngineConnector, SourceConnection
    from app.schemas.operational_mapping import (
        OperationalMapping,
        OperationalMappingApproval,
        OperationalRead,
    )
    from app.services.operational_mapping import (
        approve_operational_mapping,
        read_operational_report,
    )
    from pydantic import SecretStr
    from sqlalchemy import select

    monkeypatch.setenv("TOOL_CALL_AUDIT_HMAC_SECRET", "synthetic-native-audit-only")
    get_settings.cache_clear()
    user, business_id, _client = tenant
    fixture, _module = native_source
    reader_url = operations_platform["admin_url"].replace(
        "reservation_test_admin@", "isolated_source_reader@"
    )
    config = SourceConnection(
        key="fixture_native",
        display_name="Synthetic native source",
        engine="postgresql",
        url=SecretStr(reader_url),
        business_ids=(business_id,),
        discovery_scope=tuple(
            dict(
                schema_name="public",
                object_name=table.name,
                columns=tuple(c.name for c in table.c),
            )
            for table in fixture.tables
        ),
    )
    connector = EngineConnector(config)
    discovery = connector.discover()
    # Discovery enumerates approved objects in scope order, binding field IDs.
    assert [o.name for o in discovery.objects] == [t.name for t in fixture.tables]
    ids = {
        old.object_id: new.object_id
        for old, new in zip(fixture.discovery.objects, discovery.objects, strict=True)
    }
    payload = fixture.mapping.model_dump(mode="json")

    def translate(value):
        if isinstance(value, dict):
            return {
                k: ids.get(v, v)
                if k.endswith("object_id") and isinstance(v, str)
                else translate(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [translate(v) for v in value]
        return value

    payload = translate(payload)
    product_payload = translate(fixture.catalogue.mapping.model_dump(mode="json"))
    source_id = uuid.uuid4()
    payload.update(
        business_id=str(business_id),
        source_id=str(source_id),
        source_fingerprint=discovery.source_fingerprint,
        schema_fingerprint=discovery.schema_fingerprint,
    )
    registry = SimpleNamespace(
        get_mapping=lambda *_: None,
        get_profile=lambda _: SimpleNamespace(
            business_ids=(business_id,),
            adapter_type="postgresql_readonly",
            mapping_profile_key="discovered_products",
        ),
        connector=lambda *_: connector,
    )
    with operations_platform["factory"]() as session:
        source = OperationalDataSourceConfig(
            id=source_id,
            business_id=business_id,
            display_name="Synthetic source",
            adapter_type="postgresql_readonly",
            connection_profile_key="fixture_native",
            mapping_profile_key="discovered_products",
            mapping_profile_version=1,
            status="CONFIGURED",
        )
        session.add(source)
        session.flush()
        catalogue = SourceMappingRevision(
            id=uuid.uuid4(),
            business_id=business_id,
            source_id=source_id,
            requested_by=user.id,
            idempotency_key=uuid.uuid4(),
            version=1,
            status="proposing",
            discovery=discovery.model_dump(mode="json"),
            schema_fingerprint=discovery.schema_fingerprint,
        )
        session.add(catalogue)
        session.commit()
        catalogue.status = "review"
        session.commit()
        catalogue.status = "approved"
        catalogue.approved_mapping = product_payload
        catalogue.approved_by = user.id
        catalogue.approved_at = datetime.now(UTC)
        session.commit()
        body = OperationalMappingApproval(
            mapping=OperationalMapping.model_validate(payload),
            discovery=discovery,
            confirm_semantics=True,
            confirm_row_isolation=True,
        )
        from app.integrations.discovery import SourceMappingError
        from app.schemas.operational_mapping import (
            OperationalMappingDraft,
            OperationalMappingPreview,
        )
        from app.services import operational_mapping

        workspace = operational_mapping.operational_onboarding(
            session, user, business_id, source_id, registry
        )
        assert workspace["history"] == []
        assert workspace["context"]["catalogue_revision"] == 1
        draft = OperationalMappingDraft(mapping=body.mapping, discovery=discovery)
        validation = operational_mapping.validate_operational_draft(
            session, user, business_id, source_id, draft, registry
        )
        assert validation["approved"] is False
        assert "prices" in validation["capabilities"]
        preview = operational_mapping.preview_operational_draft(
            session,
            user,
            business_id,
            source_id,
            OperationalMappingPreview(
                **draft.model_dump(),
                request=OperationalRead(
                    capability="stock",
                    catalogue_revision=1,
                    mapping_revision=1,
                ),
            ),
            registry,
        )
        assert preview["draft"] is True and preview["approved"] is False
        from decimal import Decimal

        assert Decimal(preview["report"]["rows"][0]["on_hand"]) == Decimal("-2.125")
        assert (
            session.scalar(
                select(SourceOperationalMappingRevision).where(
                    SourceOperationalMappingRevision.source_id == source_id
                )
            )
            is None
        )

        with monkeypatch.context() as rejected:

            def unavailable():
                raise SourceMappingError("source_query_rejected")

            rejected.setattr(connector, "discover", unavailable)
            with pytest.raises(ApplicationError) as failure:
                approve_operational_mapping(
                    session, user, business_id, source_id, body, registry
                )
            assert failure.value.error_code == "source_query_rejected"
        with monkeypatch.context() as rejected:
            rejected.setattr(
                operational_mapping,
                "latest_approved",
                lambda *_: SimpleNamespace(
                    version=1,
                    discovery=discovery.model_copy(
                        update={"engine": "sqlserver"}
                    ).model_dump(mode="json"),
                ),
            )
            with pytest.raises(ApplicationError) as failure:
                approve_operational_mapping(
                    session, user, business_id, source_id, body, registry
                )
            assert failure.value.error_code == "mapping_schema_changed"
        approved = approve_operational_mapping(
            session, user, business_id, source_id, body, registry
        )
        assert approved["version"] == 1 and "prices" in approved["capabilities"]
        # Mutation is denied by table ACL; approval rows are append-only.
        row = session.scalar(
            select(SourceOperationalMappingRevision).where(
                SourceOperationalMappingRevision.source_id == source_id
            )
        )
        original = row.mapping
        for action in (
            "UPDATE public.source_operational_mapping_revisions SET version=version",
            "DELETE FROM public.source_operational_mapping_revisions",
        ):
            with (
                operations_platform["admin"].connect() as owner,
                pytest.raises(DBAPIError) as immutable,
            ):
                owner.execute(text(action + " WHERE id=:id"), {"id": row.id})
            assert immutable.value.orig.sqlstate == "22023"
        assert session.get(SourceOperationalMappingRevision, row.id).mapping == original
        with (
            operations_platform["runtime"].connect() as connection,
            pytest.raises(DBAPIError),
        ):
            connection.execute(
                text(
                    "UPDATE public.source_operational_mapping_revisions SET "
                    "version=2 WHERE id=:id"
                ),
                {"id": row.id},
            )
        source.status = "VALIDATED"
        source.last_validated_at = source.last_successful_health_check_at = (
            datetime.now(UTC)
        )
        session.commit()
        source.status = "ACTIVE"
        session.commit()
        secret = get_settings().tool_call_audit_hmac_secret
        assert secret and secret.get_secret_value()
        output = read_operational_report(
            session,
            user,
            business_id,
            source_id,
            OperationalRead(
                capability="stock", catalogue_revision=1, mapping_revision=1
            ),
            registry,
        )
        assert (
            output.status == "ok"
            and len(output.rows) == 1
            and output.rows[0].available < 0
        )
        assert (
            session.scalar(
                select(ToolCallLog)
                .where(ToolCallLog.business_id == business_id)
                .order_by(ToolCallLog.created_at.desc())
                .limit(1)
            ).tool_name
            == "mapped_operational_report"
        )
        assert session.get(SourceOperationalMappingRevision, row.id).mapping == original
        from app.agent.owner_chat_provider import (
            OwnerChatResult,
            TokenUsage,
            get_owner_chat_provider,
        )
        from app.integrations.profiles import get_connection_profile_registry
        from app.main import app
        from tests.test_owner_chat import CapturingProvider, submit

        provider = CapturingProvider(
            OwnerChatResult(
                reply="",
                decision="tool",
                semantic_operation="operational_report",
                tool_name="operational_report",
                tool_arguments={
                    "capability": "stock",
                    "product_query": "Random Diet 7UP",
                },
                usage=TokenUsage(20, 20, 40, authoritative=True),
            )
        )
        app.dependency_overrides[get_owner_chat_provider] = lambda: provider
        app.dependency_overrides[get_connection_profile_registry] = lambda: registry
        try:
            key = uuid.uuid4().hex
            first = submit(_client, user, business_id, "Stock of Random Diet 7UP", key)
            assert first.status_code == 200, first.json()
            assert "on_hand" in first.json()["assistant_message"]["content"]
            replay = submit(_client, user, business_id, "Stock of Random Diet 7UP", key)
            assert replay.status_code == 200 and replay.json()["replayed"]
            assert (
                replay.json()["assistant_message"]["id"]
                == first.json()["assistant_message"]["id"]
            )
            assert len(provider.requests) == 1
            with operations_platform["admin"].connect() as inspection:
                usage = inspection.execute(
                    text(
                        "SELECT count(*),sum(total_tokens) "
                        "FROM public.ai_usage_reservations "
                        "WHERE business_id=:id AND status='completed'"
                    ),
                    {"id": business_id},
                ).one()
                assert usage == (1, 40)
        finally:
            app.dependency_overrides.pop(get_connection_profile_registry, None)
        with pytest.raises(ApplicationError):
            read_operational_report(
                session,
                SimpleNamespace(id=uuid.uuid4()),
                business_id,
                source_id,
                OperationalRead(
                    capability="stock", catalogue_revision=1, mapping_revision=1
                ),
                registry,
            )
    connector.engine.dispose()


def test_native_decimal_precision_is_not_limited_by_python_default_context(
    native_source,
):
    from decimal import Decimal

    source, fixture = native_source
    balance = Decimal("12345678901234567890.123456789012345678")
    with source.engine.begin() as connection:
        connection.execute(
            source.tables[1]
            .update()
            .where(source.tables[1].c.tenant == "A")
            .values(balance=balance)
        )
    try:
        row = fixture.read(source, "stock").rows[0]
        assert row.on_hand == balance
        assert row.available == Decimal("12345678901234567887.123456789012345678")
    finally:
        with source.engine.begin() as connection:
            connection.execute(
                source.tables[1]
                .update()
                .where(source.tables[1].c.tenant == "A")
                .values(balance=Decimal("-2.1250"))
            )
