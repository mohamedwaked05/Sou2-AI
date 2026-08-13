"""Database-controlled business lifecycle, audit, and migration tests."""

import uuid

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.core.security import utc_now
from app.database.models import Business, BusinessLifecycleHistory, BusinessStatus
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from tests.test_business_api import (
    change_business_status,
    complete_profile,
    create_draft,
    create_user,
    headers,
)

MIGRATION_REVISION = "20260813_03"
LIFECYCLE_REVISION = "20260813_01"
PRE_LIFECYCLE_REVISION = "20260812_03"


def complete_and_confirm(
    client: TestClient, session: Session, *, email: str, name: str
) -> tuple[object, dict[str, object]]:
    user = create_user(session, email)
    business = create_draft(client, user, name)
    completed = complete_profile(client, user, str(business["id"]))
    assert completed.status_code == 200
    confirmed = client.post(
        f"/api/v1/businesses/{business['id']}/onboarding/confirm",
        headers=headers(user),
    )
    assert confirmed.status_code == 200
    return user, business


def history(engine: Engine, business_id: object) -> list[BusinessLifecycleHistory]:
    with Session(engine) as session:
        return list(
            session.scalars(
                select(BusinessLifecycleHistory)
                .where(BusinessLifecycleHistory.business_id == business_id)
                .order_by(BusinessLifecycleHistory.changed_at)
            )
        )


def assert_rejected_without_audit(
    session: Session,
    migration_engine: Engine,
    business_id: object,
    new_status: str,
    *,
    admin_identifier: str = "test:operator",
    reason: str = "Rejected lifecycle test",
    message: str,
) -> None:
    with Session(migration_engine) as audit_session:
        before = audit_session.scalar(
            select(func.count())
            .select_from(BusinessLifecycleHistory)
            .where(BusinessLifecycleHistory.business_id == business_id)
        )
    with pytest.raises(DBAPIError, match=message):
        change_business_status(
            session,
            business_id,
            new_status,
            admin_identifier=admin_identifier,
            reason=reason,
        )
    session.rollback()
    with Session(migration_engine) as audit_session:
        after = audit_session.scalar(
            select(func.count())
            .select_from(BusinessLifecycleHistory)
            .where(BusinessLifecycleHistory.business_id == business_id)
        )
    assert after == before


def test_lifecycle_transitions_derive_api_activity_and_write_exact_audits(
    api_client: TestClient, db_session: Session, migration_engine: Engine
) -> None:
    user, business = complete_and_confirm(
        api_client,
        db_session,
        email="lifecycle@example.com",
        name="Lifecycle Market",
    )
    path = f"/api/v1/businesses/{business['id']}"
    pending = api_client.get(path, headers=headers(user)).json()
    assert pending["status"] == "PENDING"
    assert pending["is_active"] is False

    activated = change_business_status(
        db_session,
        business["id"],
        "ACTIVE",
        admin_identifier="  operator@example.com  ",
        reason="  Offline payment received  ",
    )
    assert activated.status == BusinessStatus.ACTIVE
    active = api_client.get(path, headers=headers(user)).json()
    assert active["status"] == "ACTIVE"
    assert active["is_active"] is True

    disabled = change_business_status(
        db_session, business["id"], "DISABLED", reason="Payment expired"
    )
    assert disabled.status == BusinessStatus.DISABLED
    disabled_response = api_client.get(path, headers=headers(user)).json()
    assert disabled_response["status"] == "DISABLED"
    assert disabled_response["is_active"] is False

    reenabled = change_business_status(
        db_session, business["id"], "ACTIVE", reason="Payment renewed"
    )
    assert reenabled.status == BusinessStatus.ACTIVE

    records = history(migration_engine, business["id"])
    assert [(row.previous_status, row.new_status) for row in records] == [
        (BusinessStatus.PENDING, BusinessStatus.ACTIVE),
        (BusinessStatus.ACTIVE, BusinessStatus.DISABLED),
        (BusinessStatus.DISABLED, BusinessStatus.ACTIVE),
    ]
    assert records[0].admin_identifier == "operator@example.com"
    assert records[0].reason == "Offline payment received"
    assert all(row.changed_at.tzinfo is not None for row in records)


def test_activation_requires_complete_confirmed_current_profile(
    api_client: TestClient, db_session: Session, migration_engine: Engine
) -> None:
    user = create_user(db_session, "eligibility-lifecycle@example.com")
    incomplete = create_draft(api_client, user, "Incomplete Lifecycle")
    business = db_session.get(Business, uuid.UUID(str(incomplete["id"])))
    assert business is not None
    business.onboarding_submitted_at = utc_now()
    db_session.commit()
    assert_rejected_without_audit(
        db_session,
        migration_engine,
        business.id,
        "ACTIVE",
        message="complete confirmed profile",
    )

    unconfirmed = create_draft(api_client, user, "Unconfirmed Lifecycle")
    assert complete_profile(api_client, user, str(unconfirmed["id"])).status_code == 200
    assert_rejected_without_audit(
        db_session,
        migration_engine,
        unconfirmed["id"],
        "ACTIVE",
        message="complete confirmed profile",
    )


def test_reenable_revalidates_disabled_business_profile(
    api_client: TestClient, db_session: Session, migration_engine: Engine
) -> None:
    user, business = complete_and_confirm(
        api_client,
        db_session,
        email="reenable@example.com",
        name="Reenable Market",
    )
    change_business_status(db_session, business["id"], "ACTIVE")
    change_business_status(db_session, business["id"], "DISABLED")
    changed = api_client.patch(
        f"/api/v1/businesses/{business['id']}",
        headers=headers(user),
        json={"description": None},
    )
    assert changed.status_code == 200
    assert changed.json()["profile_complete"] is False
    assert_rejected_without_audit(
        db_session,
        migration_engine,
        business["id"],
        "ACTIVE",
        message="complete confirmed profile",
    )


def test_disabled_business_cannot_use_owner_chat(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = complete_and_confirm(
        api_client,
        db_session,
        email="disabled-chat@example.com",
        name="Disabled Chat Market",
    )
    change_business_status(db_session, business["id"], "ACTIVE")
    change_business_status(db_session, business["id"], "DISABLED")

    response = api_client.post(
        f"/api/v1/businesses/{business['id']}/owner-chat/messages",
        headers=headers(user),
        json={"idempotency_key": "disabled", "content": "Hello"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "business_not_active"
    assert response.json()["error"]["message"] == "This business is not active."
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


@pytest.mark.parametrize(
    ("admin_identifier", "reason", "message"),
    [
        ("   ", "Valid reason", "Admin identifier"),
        ("operator", "   ", "Reason"),
        ("x" * 321, "Valid reason", "Admin identifier"),
        ("operator", "x" * 2001, "Reason"),
    ],
)
def test_required_bounded_administrative_fields(
    api_client: TestClient,
    db_session: Session,
    migration_engine: Engine,
    admin_identifier: str,
    reason: str,
    message: str,
) -> None:
    _, business = complete_and_confirm(
        api_client,
        db_session,
        email=f"admin-{uuid.uuid4()}@example.com",
        name=f"Admin {uuid.uuid4()}",
    )
    assert_rejected_without_audit(
        db_session,
        migration_engine,
        business["id"],
        "ACTIVE",
        admin_identifier=admin_identifier,
        reason=reason,
        message=message,
    )


def test_same_forbidden_and_nonexistent_transitions_create_no_audit(
    api_client: TestClient, db_session: Session, migration_engine: Engine
) -> None:
    _, pending = complete_and_confirm(
        api_client,
        db_session,
        email="forbidden-pending@example.com",
        name="Forbidden Pending",
    )
    assert_rejected_without_audit(
        db_session,
        migration_engine,
        pending["id"],
        "PENDING",
        message="status must change",
    )
    assert_rejected_without_audit(
        db_session,
        migration_engine,
        pending["id"],
        "DISABLED",
        message="transition is not allowed",
    )

    change_business_status(db_session, pending["id"], "ACTIVE")
    assert_rejected_without_audit(
        db_session,
        migration_engine,
        pending["id"],
        "PENDING",
        message="transition is not allowed",
    )
    change_business_status(db_session, pending["id"], "DISABLED")
    assert_rejected_without_audit(
        db_session,
        migration_engine,
        pending["id"],
        "PENDING",
        message="transition is not allowed",
    )
    assert_rejected_without_audit(
        db_session,
        migration_engine,
        pending["id"],
        "DISABLED",
        message="status must change",
    )
    assert_rejected_without_audit(
        db_session,
        migration_engine,
        uuid.uuid4(),
        "ACTIVE",
        message="Business was not found",
    )


def test_runtime_role_cannot_bypass_lifecycle_privileges(
    api_client: TestClient,
    db_session: Session,
    migration_engine: Engine,
) -> None:
    _, business = complete_and_confirm(
        api_client,
        db_session,
        email="bypass@example.com",
        name="Bypass Market",
    )
    former_settings = {
        "sou2ai.lifecycle_business_id": str(business["id"]),
        "sou2ai.lifecycle_previous_status": "PENDING",
        "sou2ai.lifecycle_new_status": "ACTIVE",
    }
    for setting_name, setting_value in former_settings.items():
        db_session.execute(
            text("SELECT set_config(:name, :value, true)"),
            {"name": setting_name, "value": setting_value},
        )
    with pytest.raises(DBAPIError, match="permission denied"):
        db_session.execute(
            text("UPDATE businesses SET status = 'ACTIVE' WHERE id = :id"),
            {"id": business["id"]},
        )
    db_session.rollback()

    with pytest.raises(DBAPIError, match="permission denied"):
        db_session.execute(
            text(
                "SELECT * FROM public.sou2ai_change_business_status("
                ":id, 'ACTIVE'::business_status, 'runtime', 'forbidden')"
            ),
            {"id": business["id"]},
        )
    db_session.rollback()

    change_business_status(db_session, business["id"], "ACTIVE")
    record = history(migration_engine, business["id"])[0]

    for setting_name, setting_value in former_settings.items():
        db_session.execute(
            text("SELECT set_config(:name, :value, true)"),
            {"name": setting_name, "value": setting_value},
        )
    with pytest.raises(DBAPIError, match="permission denied"):
        db_session.execute(
            text(
                "INSERT INTO business_lifecycle_history "
                "(id, business_id, previous_status, new_status, "
                "admin_identifier, reason) VALUES "
                "(:id, :business_id, 'PENDING', 'ACTIVE', 'bypass', 'bypass')"
            ),
            {"id": uuid.uuid4(), "business_id": business["id"]},
        )
    db_session.rollback()
    with pytest.raises(DBAPIError, match="permission denied"):
        db_session.execute(
            text(
                "UPDATE business_lifecycle_history SET reason = 'Changed' "
                "WHERE id = :id"
            ),
            {"id": record.id},
        )
    db_session.rollback()
    with pytest.raises(DBAPIError, match="permission denied"):
        db_session.execute(
            text("DELETE FROM business_lifecycle_history WHERE id = :id"),
            {"id": record.id},
        )
    db_session.rollback()
    with pytest.raises(DBAPIError, match="permission denied"):
        db_session.execute(text("TRUNCATE business_lifecycle_history"))
    db_session.rollback()


def test_operator_can_execute_only_the_controlled_function(
    api_client: TestClient,
    db_session: Session,
    migration_engine: Engine,
    operator_engine: Engine,
) -> None:
    _, business = complete_and_confirm(
        api_client,
        db_session,
        email="operator-boundary@example.com",
        name="Operator Boundary",
    )
    with operator_engine.begin() as connection:
        assert connection.scalar(text("SELECT current_user")) == (
            "sou2ai_lifecycle_operator_login"
        )
        result = connection.execute(
            text(
                "SELECT * FROM public.sou2ai_change_business_status("
                ":id, 'ACTIVE'::business_status, 'test:operator', "
                "'Verified operator execution')"
            ),
            {"id": business["id"]},
        ).one()
        assert result.status == "ACTIVE"

    records = history(migration_engine, business["id"])
    assert len(records) == 1
    assert records[0].previous_status is BusinessStatus.PENDING
    assert records[0].new_status is BusinessStatus.ACTIVE
    assert records[0].admin_identifier == "test:operator"
    assert records[0].reason == "Verified operator execution"

    forbidden_statements = (
        "UPDATE public.businesses SET status = 'DISABLED' WHERE id = :id",
        "INSERT INTO public.business_lifecycle_history "
        "(id, business_id, previous_status, new_status, admin_identifier, reason) "
        "VALUES (gen_random_uuid(), :id, 'ACTIVE', 'DISABLED', 'x', 'x')",
        "UPDATE public.business_lifecycle_history SET reason = 'x' "
        "WHERE business_id = :id",
        "DELETE FROM public.business_lifecycle_history WHERE business_id = :id",
        "TRUNCATE public.business_lifecycle_history",
    )
    for statement in forbidden_statements:
        with pytest.raises(DBAPIError, match="permission denied"):
            with operator_engine.begin() as connection:
                connection.execute(text(statement), {"id": business["id"]})


def test_lifecycle_function_acl_owner_and_search_path_are_hardened(
    migration_engine: Engine,
) -> None:
    signature = (
        "public.sou2ai_change_business_status(uuid,public.business_status,text,text)"
    )
    with migration_engine.connect() as connection:
        function = connection.execute(
            text(
                "SELECT owner.rolname, procedure.prosecdef, procedure.proconfig, "
                "pg_get_functiondef(procedure.oid) AS definition, "
                "EXISTS (SELECT 1 FROM aclexplode(procedure.proacl) acl "
                "WHERE acl.grantee = 0) AS public_acl "
                "FROM pg_proc procedure "
                "JOIN pg_roles owner ON owner.oid = procedure.proowner "
                "WHERE procedure.oid = CAST(:signature AS regprocedure)"
            ),
            {"signature": signature},
        ).one()
        assert function.rolname == "sou2ai_migrator"
        assert function.prosecdef is True
        assert function.proconfig == ["search_path=pg_catalog"]
        assert function.public_acl is False
        assert "set_config" not in function.definition
        assert "public.businesses" in function.definition
        assert "public.business_lifecycle_history" in function.definition

        roles = {
            row.rolname: row
            for row in connection.execute(
                text(
                    "SELECT rolname, rolsuper, rolcanlogin, rolcreaterole "
                    "FROM pg_roles WHERE rolname IN "
                    "('sou2ai_migrator', 'sou2ai_runtime', "
                    "'sou2ai_lifecycle_operator')"
                )
            )
        }
        assert set(roles) == {
            "sou2ai_migrator",
            "sou2ai_runtime",
            "sou2ai_lifecycle_operator",
        }
        assert all(not role.rolsuper for role in roles.values())
        assert all(not role.rolcanlogin for role in roles.values())
        assert all(not role.rolcreaterole for role in roles.values())
        assert (
            connection.scalar(
                text(
                    "SELECT pg_has_role('sou2ai_lifecycle_operator_login', "
                    "'sou2ai_migrator', 'MEMBER')"
                )
            )
            is False
        )
        assert (
            connection.scalar(
                text(
                    "SELECT pg_has_role('sou2ai_lifecycle_operator_login', "
                    "'sou2ai_runtime', 'MEMBER')"
                )
            )
            is False
        )

        assert (
            connection.scalar(
                text("SELECT has_function_privilege(:role, :signature, 'EXECUTE')"),
                {"role": "sou2ai_runtime", "signature": signature},
            )
            is False
        )
        assert (
            connection.scalar(
                text("SELECT has_function_privilege(:role, :signature, 'EXECUTE')"),
                {"role": "sou2ai_lifecycle_operator", "signature": signature},
            )
            is True
        )

        owners = dict(
            connection.execute(
                text(
                    "SELECT class.relname, role.rolname FROM pg_class class "
                    "JOIN pg_roles role ON role.oid = class.relowner "
                    "WHERE class.relname IN "
                    "('businesses', 'business_lifecycle_history')"
                )
            ).all()
        )
        assert owners == {
            "businesses": "sou2ai_migrator",
            "business_lifecycle_history": "sou2ai_migrator",
        }
        assert (
            connection.scalar(
                text(
                    "SELECT has_column_privilege("
                    "'sou2ai_runtime', 'public.businesses', 'status', 'UPDATE')"
                )
            )
            is False
        )
        assert (
            connection.scalar(
                text(
                    "SELECT has_column_privilege("
                    "'sou2ai_runtime', 'public.businesses', 'description', 'UPDATE')"
                )
            )
            is True
        )
        assert (
            connection.scalar(
                text(
                    "SELECT has_schema_privilege('sou2ai_runtime', 'public', 'CREATE')"
                )
            )
            is False
        )
        assert (
            connection.scalar(
                text(
                    "SELECT has_schema_privilege("
                    "'sou2ai_lifecycle_operator', 'public', 'CREATE')"
                )
            )
            is False
        )
        for role in ("sou2ai_runtime", "sou2ai_lifecycle_operator"):
            for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                assert (
                    connection.scalar(
                        text(
                            "SELECT has_table_privilege("
                            ":role, 'public.business_lifecycle_history', :privilege)"
                        ),
                        {"role": role, "privilege": privilege},
                    )
                    is False
                )


def test_runtime_login_is_non_superuser_and_has_no_privileged_membership(
    database_engine: Engine,
) -> None:
    with database_engine.connect() as connection:
        role = connection.execute(
            text(
                "SELECT current_user, role.rolsuper, role.rolcreaterole, "
                "pg_has_role(current_user, 'sou2ai_runtime', 'MEMBER') AS runtime, "
                "pg_has_role(current_user, 'sou2ai_migrator', 'MEMBER') AS migrator, "
                "pg_has_role(current_user, 'sou2ai_lifecycle_operator', 'MEMBER') "
                "AS operator FROM pg_roles role WHERE role.rolname = current_user"
            )
        ).one()
    assert role.current_user == "sou2ai_runtime_login"
    assert role.rolsuper is False
    assert role.rolcreaterole is False
    assert role.runtime is True
    assert role.migrator is False
    assert role.operator is False


def test_lifecycle_update_and_audit_insert_are_atomic(
    api_client: TestClient, db_session: Session, migration_engine: Engine
) -> None:
    _, business = complete_and_confirm(
        api_client,
        db_session,
        email="atomic@example.com",
        name="Atomic Lifecycle",
    )
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE FUNCTION public.test_reject_lifecycle_audit() "
                "RETURNS trigger AS $$ BEGIN RAISE EXCEPTION "
                "'forced audit failure'; END; $$ LANGUAGE plpgsql; "
                "CREATE TRIGGER test_reject_lifecycle_audit BEFORE INSERT "
                "ON public.business_lifecycle_history FOR EACH ROW "
                "EXECUTE FUNCTION public.test_reject_lifecycle_audit()"
            )
        )
    try:
        with pytest.raises(DBAPIError, match="forced audit failure"):
            change_business_status(db_session, business["id"], "ACTIVE")
        db_session.rollback()
        current = db_session.get(Business, uuid.UUID(str(business["id"])))
        assert current is not None
        assert current.status is BusinessStatus.PENDING
        assert history(migration_engine, business["id"]) == []
    finally:
        with migration_engine.begin() as connection:
            connection.execute(
                text(
                    "DROP TRIGGER IF EXISTS test_reject_lifecycle_audit "
                    "ON public.business_lifecycle_history; "
                    "DROP FUNCTION IF EXISTS public.test_reject_lifecycle_audit()"
                )
            )


def test_lifecycle_schema_has_one_head_and_no_legacy_active_column(
    database_engine: Engine, alembic_config: Config
) -> None:
    inspector = inspect(database_engine)
    assert "business_lifecycle_history" in inspector.get_table_names()
    assert "is_active" not in {
        column["name"] for column in inspector.get_columns("businesses")
    }
    heads = ScriptDirectory.from_config(alembic_config).get_heads()
    assert heads == [MIGRATION_REVISION]
    with database_engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM pg_proc "
                    "WHERE proname = 'sou2ai_change_business_status' "
                    "AND pg_get_function_identity_arguments(oid) = "
                    "'target_business_id uuid, requested_status business_status, "
                    "admin_identifier text, reason text'"
                )
            )
            == 1
        )


def test_lifecycle_migration_backfills_legacy_rows_and_round_trips(
    database_engine: Engine,
    migration_engine: Engine,
    alembic_config: Config,
) -> None:
    active_id = uuid.uuid4()
    pending_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    database_engine.dispose()
    migration_engine.dispose()
    try:
        command.downgrade(alembic_config, PRE_LIFECYCLE_REVISION)
        with migration_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, email, first_name, last_name, password_hash) VALUES "
                    "(:id, :email, 'Migration', 'Owner', 'hash')"
                ),
                {"id": owner_id, "email": f"migration-{owner_id}@example.com"},
            )
            for business_id, name, is_active in (
                (active_id, "Legacy Active", True),
                (pending_id, "Legacy Pending", False),
            ):
                connection.execute(
                    text(
                        "INSERT INTO businesses "
                        "(id, owner_user_id, name, normalized_name, is_active) "
                        "VALUES (:id, :owner, :name, :normalized, :active)"
                    ),
                    {
                        "id": business_id,
                        "owner": owner_id,
                        "name": name,
                        "normalized": name.casefold(),
                        "active": is_active,
                    },
                )
        command.upgrade(alembic_config, "head")
        with migration_engine.connect() as connection:
            rows = dict(
                connection.execute(
                    text(
                        "SELECT id, status::text FROM businesses "
                        "WHERE id IN (:active_id, :pending_id)"
                    ),
                    {"active_id": active_id, "pending_id": pending_id},
                ).all()
            )
            assert rows == {active_id: "ACTIVE", pending_id: "PENDING"}
            audit = connection.execute(
                text(
                    "SELECT business_id, previous_status::text, new_status::text, "
                    "admin_identifier, reason FROM business_lifecycle_history "
                    "WHERE business_id IN (:active_id, :pending_id)"
                ),
                {"active_id": active_id, "pending_id": pending_id},
            ).all()
            assert audit == [
                (
                    active_id,
                    "PENDING",
                    "ACTIVE",
                    "system:migration",
                    "Migrated from legacy active state",
                )
            ]
            assert "is_active" not in {
                column["name"]
                for column in inspect(connection).get_columns("businesses")
            }
    finally:
        command.upgrade(alembic_config, "head")


def test_security_migration_downgrade_upgrade_preserves_history(
    database_engine: Engine,
    migration_engine: Engine,
    alembic_config: Config,
) -> None:
    database_engine.dispose()
    migration_engine.dispose()
    with migration_engine.connect() as connection:
        before = connection.scalar(
            text("SELECT count(*) FROM public.business_lifecycle_history")
        )
    try:
        command.downgrade(alembic_config, LIFECYCLE_REVISION)
        with migration_engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM public.business_lifecycle_history")
                )
                == before
            )
        command.upgrade(alembic_config, "head")
        with migration_engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM public.business_lifecycle_history")
                )
                == before
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT role.rolname FROM pg_class class "
                        "JOIN pg_roles role ON role.oid = class.relowner "
                        "WHERE class.oid = 'public.businesses'::regclass"
                    )
                )
                == "sou2ai_migrator"
            )
    finally:
        command.upgrade(alembic_config, "head")
