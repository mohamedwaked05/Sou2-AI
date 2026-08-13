"""Secure lifecycle writes with PostgreSQL privilege separation.

Revision ID: 20260813_02
Revises: 20260813_01
Create Date: 2026-08-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260813_02"
down_revision: str | None = "20260813_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATOR_ROLE = "sou2ai_migrator"
RUNTIME_ROLE = "sou2ai_runtime"
OPERATOR_ROLE = "sou2ai_lifecycle_operator"

APPLICATION_TABLES = (
    "users",
    "refresh_sessions",
    "email_verification_tokens",
    "password_reset_tokens",
    "authentication_events",
    "authentication_maintenance_tasks",
    "business_memberships",
    "business_opening_days",
    "business_opening_shifts",
    "owner_conversations",
    "owner_chat_messages",
    "business_knowledge",
    "tool_call_logs",
)

RUNTIME_BUSINESS_UPDATE_COLUMNS = (
    "name",
    "normalized_name",
    "description",
    "category",
    "custom_category",
    "country",
    "timezone",
    "governorate",
    "district",
    "city",
    "address_line",
    "onboarding_submitted_at",
    "updated_at",
)


def _create_roles() -> None:
    op.execute(
        f"""
        DO $roles$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles
                           WHERE rolname = '{MIGRATOR_ROLE}') THEN
                CREATE ROLE {MIGRATOR_ROLE}
                    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles
                           WHERE rolname = '{RUNTIME_ROLE}') THEN
                CREATE ROLE {RUNTIME_ROLE}
                    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles
                           WHERE rolname = '{OPERATOR_ROLE}') THEN
                CREATE ROLE {OPERATOR_ROLE}
                    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
            END IF;
        END
        $roles$;
        """
    )
    op.execute(
        f"ALTER ROLE {MIGRATOR_ROLE} "
        "NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
    )
    op.execute(
        f"ALTER ROLE {RUNTIME_ROLE} "
        "NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
    )
    op.execute(
        f"ALTER ROLE {OPERATOR_ROLE} "
        "NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
    )


def _drop_forgeable_controls() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_businesses_lifecycle_status ON public.businesses"
    )
    op.execute("DROP FUNCTION IF EXISTS public.sou2ai_guard_business_status_update()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_business_lifecycle_history_append_only "
        "ON public.business_lifecycle_history"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_business_lifecycle_history_no_truncate "
        "ON public.business_lifecycle_history"
    )
    op.execute("DROP FUNCTION IF EXISTS public.sou2ai_guard_lifecycle_history()")


def _create_append_only_history_guards() -> None:
    op.execute(
        """
        CREATE FUNCTION public.sou2ai_guard_lifecycle_history_append_only()
        RETURNS trigger AS $function$
        BEGIN
            RAISE EXCEPTION 'Business lifecycle history is append-only.'
                USING ERRCODE = '55000';
        END;
        $function$ LANGUAGE plpgsql
        SET search_path = pg_catalog;

        CREATE TRIGGER trg_business_lifecycle_history_append_only
        BEFORE UPDATE OR DELETE ON public.business_lifecycle_history
        FOR EACH ROW
        EXECUTE FUNCTION public.sou2ai_guard_lifecycle_history_append_only();

        CREATE TRIGGER trg_business_lifecycle_history_no_truncate
        BEFORE TRUNCATE ON public.business_lifecycle_history
        FOR EACH STATEMENT
        EXECUTE FUNCTION public.sou2ai_guard_lifecycle_history_append_only();
        """
    )


def _create_secure_lifecycle_function() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.sou2ai_change_business_status(
            target_business_id uuid,
            requested_status public.business_status,
            admin_identifier text,
            reason text
        ) RETURNS TABLE(business_id uuid, status public.business_status)
        AS $function$
        DECLARE
            previous_status public.business_status;
            clean_admin_identifier text := pg_catalog.btrim(admin_identifier);
            clean_reason text := pg_catalog.btrim(reason);
        BEGIN
            IF clean_admin_identifier IS NULL
               OR pg_catalog.char_length(clean_admin_identifier) NOT BETWEEN 1 AND 320
            THEN
                RAISE EXCEPTION
                    'Admin identifier must contain between 1 and 320 characters.'
                    USING ERRCODE = '22023';
            END IF;
            IF clean_reason IS NULL
               OR pg_catalog.char_length(clean_reason) NOT BETWEEN 1 AND 2000 THEN
                RAISE EXCEPTION
                    'Reason must contain between 1 and 2000 characters.'
                    USING ERRCODE = '22023';
            END IF;
            IF requested_status IS NULL THEN
                RAISE EXCEPTION 'Requested business status is required.'
                    USING ERRCODE = '22023';
            END IF;

            SELECT business.status INTO previous_status
            FROM public.businesses AS business
            WHERE business.id = target_business_id
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Business was not found.' USING ERRCODE = 'P0002';
            END IF;
            IF previous_status = requested_status THEN
                RAISE EXCEPTION 'Business status must change.'
                    USING ERRCODE = '23514';
            END IF;
            IF NOT (
                (previous_status = 'PENDING' AND requested_status = 'ACTIVE')
                OR (previous_status = 'ACTIVE' AND requested_status = 'DISABLED')
                OR (previous_status = 'DISABLED' AND requested_status = 'ACTIVE')
            ) THEN
                RAISE EXCEPTION 'Business lifecycle transition is not allowed.'
                    USING ERRCODE = '23514';
            END IF;
            IF requested_status = 'ACTIVE'
               AND (
                    NOT public.sou2ai_business_profile_complete(target_business_id)
                    OR NOT EXISTS (
                        SELECT 1 FROM public.businesses
                        WHERE id = target_business_id
                          AND onboarding_submitted_at IS NOT NULL
                    )
               ) THEN
                RAISE EXCEPTION
                    'Business must have a complete confirmed profile before activation.'
                    USING ERRCODE = '23514';
            END IF;

            UPDATE public.businesses
            SET status = requested_status
            WHERE id = target_business_id;

            INSERT INTO public.business_lifecycle_history (
                id,
                business_id,
                previous_status,
                new_status,
                admin_identifier,
                reason
            ) VALUES (
                pg_catalog.gen_random_uuid(),
                target_business_id,
                previous_status,
                requested_status,
                clean_admin_identifier,
                clean_reason
            );

            RETURN QUERY SELECT target_business_id, requested_status;
        END;
        $function$ LANGUAGE plpgsql
           SECURITY DEFINER
           SET search_path = pg_catalog;
        """
    )


def _secure_ownership_and_privileges() -> None:
    application_tables = ", ".join(f"public.{name}" for name in APPLICATION_TABLES)
    update_columns = ", ".join(RUNTIME_BUSINESS_UPDATE_COLUMNS)
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {RUNTIME_ROLE}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {OPERATOR_ROLE}")
    op.execute(f"GRANT USAGE, CREATE ON SCHEMA public TO {MIGRATOR_ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE}, {OPERATOR_ROLE}")
    op.execute(
        "ALTER FUNCTION public.sou2ai_business_profile_complete(uuid) "
        "SET search_path = pg_catalog, public"
    )

    op.execute(f"ALTER TABLE public.businesses OWNER TO {MIGRATOR_ROLE}")
    op.execute(
        f"ALTER TABLE public.business_lifecycle_history OWNER TO {MIGRATOR_ROLE}"
    )
    op.execute(f"ALTER TYPE public.business_status OWNER TO {MIGRATOR_ROLE}")
    for signature in (
        "public.sou2ai_guard_business_initial_status()",
        "public.sou2ai_guard_lifecycle_history_append_only()",
        "public.sou2ai_change_business_status(uuid, public.business_status, text, text)",
    ):
        op.execute(f"ALTER FUNCTION {signature} OWNER TO {MIGRATOR_ROLE}")

    op.execute("REVOKE ALL ON TABLE public.businesses FROM PUBLIC")
    op.execute("REVOKE ALL ON TABLE public.business_lifecycle_history FROM PUBLIC")
    op.execute(f"REVOKE ALL ON TABLE public.businesses FROM {RUNTIME_ROLE}")
    op.execute(
        f"REVOKE ALL ON TABLE public.business_lifecycle_history FROM {RUNTIME_ROLE}"
    )
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {OPERATOR_ROLE}")

    op.execute(f"GRANT SELECT, INSERT ON TABLE public.businesses TO {RUNTIME_ROLE}")
    op.execute(
        f"GRANT UPDATE ({update_columns}) ON TABLE public.businesses TO {RUNTIME_ROLE}"
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {application_tables} "
        f"TO {RUNTIME_ROLE}"
    )
    op.execute(
        "GRANT SELECT ON TABLE public.business_opening_days, "
        f"public.business_opening_shifts TO {MIGRATOR_ROLE}"
    )
    op.execute(
        "GRANT USAGE ON TYPE public.account_status, public.default_language, "
        "public.tool_call_status, public.business_status, "
        "public.membership_permission, public.business_category "
        f"TO {RUNTIME_ROLE}"
    )
    op.execute(f"GRANT USAGE ON TYPE public.business_status TO {OPERATOR_ROLE}")

    function_signature = (
        "public.sou2ai_change_business_status(uuid, public.business_status, text, text)"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {function_signature} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION {function_signature} FROM {RUNTIME_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {function_signature} TO {OPERATOR_ROLE}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {MIGRATOR_ROLE} IN SCHEMA public "
        "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
    )


def upgrade() -> None:
    _create_roles()
    _drop_forgeable_controls()
    _create_append_only_history_guards()
    _create_secure_lifecycle_function()
    _secure_ownership_and_privileges()


def _restore_guc_lifecycle_controls() -> None:
    op.execute(
        """
        CREATE FUNCTION public.sou2ai_guard_business_status_update()
        RETURNS trigger AS $function$
        BEGIN
            IF NEW.status IS DISTINCT FROM OLD.status
               AND (
                    current_setting('sou2ai.lifecycle_business_id', true)
                        IS DISTINCT FROM NEW.id::text
                    OR current_setting('sou2ai.lifecycle_previous_status', true)
                        IS DISTINCT FROM OLD.status::text
                    OR current_setting('sou2ai.lifecycle_new_status', true)
                        IS DISTINCT FROM NEW.status::text
               ) THEN
                RAISE EXCEPTION
                    'Business status changes must use sou2ai_change_business_status.'
                    USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END;
        $function$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_businesses_lifecycle_status
        BEFORE UPDATE OF status ON public.businesses
        FOR EACH ROW EXECUTE FUNCTION public.sou2ai_guard_business_status_update();

        CREATE FUNCTION public.sou2ai_guard_lifecycle_history() RETURNS trigger
        AS $function$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE', 'TRUNCATE') THEN
                RAISE EXCEPTION 'Business lifecycle history is append-only.'
                    USING ERRCODE = '55000';
            END IF;
            IF current_setting('sou2ai.lifecycle_business_id', true)
                    IS DISTINCT FROM NEW.business_id::text
               OR current_setting('sou2ai.lifecycle_previous_status', true)
                    IS DISTINCT FROM NEW.previous_status::text
               OR current_setting('sou2ai.lifecycle_new_status', true)
                    IS DISTINCT FROM NEW.new_status::text THEN
                RAISE EXCEPTION
                    'Business lifecycle history is written only by the lifecycle function.'
                    USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END;
        $function$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_business_lifecycle_history_append_only
        BEFORE INSERT OR UPDATE OR DELETE ON public.business_lifecycle_history
        FOR EACH ROW EXECUTE FUNCTION public.sou2ai_guard_lifecycle_history();

        CREATE TRIGGER trg_business_lifecycle_history_no_truncate
        BEFORE TRUNCATE ON public.business_lifecycle_history
        FOR EACH STATEMENT EXECUTE FUNCTION public.sou2ai_guard_lifecycle_history();
        """
    )


def _restore_guc_lifecycle_function() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.sou2ai_change_business_status(
            target_business_id uuid,
            requested_status public.business_status,
            admin_identifier text,
            reason text
        ) RETURNS TABLE(business_id uuid, status public.business_status)
        AS $function$
        DECLARE
            previous_status public.business_status;
            clean_admin_identifier text := btrim(admin_identifier);
            clean_reason text := btrim(reason);
        BEGIN
            IF clean_admin_identifier IS NULL
               OR char_length(clean_admin_identifier) NOT BETWEEN 1 AND 320 THEN
                RAISE EXCEPTION
                    'Admin identifier must contain between 1 and 320 characters.'
                    USING ERRCODE = '22023';
            END IF;
            IF clean_reason IS NULL
               OR char_length(clean_reason) NOT BETWEEN 1 AND 2000 THEN
                RAISE EXCEPTION
                    'Reason must contain between 1 and 2000 characters.'
                    USING ERRCODE = '22023';
            END IF;
            IF requested_status IS NULL THEN
                RAISE EXCEPTION 'Requested business status is required.'
                    USING ERRCODE = '22023';
            END IF;

            SELECT business.status INTO previous_status
            FROM public.businesses AS business
            WHERE business.id = target_business_id
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Business was not found.' USING ERRCODE = 'P0002';
            END IF;
            IF previous_status = requested_status THEN
                RAISE EXCEPTION 'Business status must change.'
                    USING ERRCODE = '23514';
            END IF;
            IF NOT (
                (previous_status = 'PENDING' AND requested_status = 'ACTIVE')
                OR (previous_status = 'ACTIVE' AND requested_status = 'DISABLED')
                OR (previous_status = 'DISABLED' AND requested_status = 'ACTIVE')
            ) THEN
                RAISE EXCEPTION 'Business lifecycle transition is not allowed.'
                    USING ERRCODE = '23514';
            END IF;
            IF requested_status = 'ACTIVE'
               AND (
                    NOT public.sou2ai_business_profile_complete(target_business_id)
                    OR NOT EXISTS (
                        SELECT 1 FROM public.businesses
                        WHERE id = target_business_id
                          AND onboarding_submitted_at IS NOT NULL
                    )
               ) THEN
                RAISE EXCEPTION
                    'Business must have a complete confirmed profile before activation.'
                    USING ERRCODE = '23514';
            END IF;

            PERFORM set_config(
                'sou2ai.lifecycle_business_id', target_business_id::text, true
            );
            PERFORM set_config(
                'sou2ai.lifecycle_previous_status', previous_status::text, true
            );
            PERFORM set_config(
                'sou2ai.lifecycle_new_status', requested_status::text, true
            );

            UPDATE public.businesses SET status = requested_status
            WHERE id = target_business_id;
            INSERT INTO public.business_lifecycle_history (
                id, business_id, previous_status, new_status,
                admin_identifier, reason
            ) VALUES (
                gen_random_uuid(), target_business_id, previous_status,
                requested_status, clean_admin_identifier, clean_reason
            );
            RETURN QUERY SELECT target_business_id, requested_status;
        END;
        $function$ LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = public, pg_temp;
        """
    )


def downgrade() -> None:
    bootstrap_owner = op.get_bind().exec_driver_sql("SELECT session_user").scalar_one()
    quoted_owner = op.get_bind().dialect.identifier_preparer.quote(bootstrap_owner)

    _drop_forgeable_controls()
    op.execute(
        "DROP FUNCTION IF EXISTS public.sou2ai_guard_lifecycle_history_append_only()"
    )

    op.execute(f"ALTER TABLE public.businesses OWNER TO {quoted_owner}")
    op.execute(f"ALTER TABLE public.business_lifecycle_history OWNER TO {quoted_owner}")
    op.execute(f"ALTER TYPE public.business_status OWNER TO {quoted_owner}")
    op.execute(
        "ALTER FUNCTION public.sou2ai_guard_business_initial_status() "
        f"OWNER TO {quoted_owner}"
    )
    op.execute(
        "ALTER FUNCTION public.sou2ai_business_profile_complete(uuid) RESET search_path"
    )

    _restore_guc_lifecycle_controls()
    _restore_guc_lifecycle_function()
    op.execute(
        "ALTER FUNCTION public.sou2ai_change_business_status("
        "uuid, public.business_status, text, text) "
        f"OWNER TO {quoted_owner}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.sou2ai_change_business_status("
        "uuid, public.business_status, text, text) "
        f"FROM PUBLIC, {RUNTIME_ROLE}, {OPERATOR_ROLE}"
    )
    op.execute(
        "REVOKE SELECT ON TABLE public.business_opening_days, "
        f"public.business_opening_shifts FROM {MIGRATOR_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {MIGRATOR_ROLE} IN SCHEMA public "
        "GRANT EXECUTE ON FUNCTIONS TO PUBLIC"
    )
