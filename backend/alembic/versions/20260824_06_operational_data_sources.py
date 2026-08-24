"""Add tenant-scoped operational data source management.

Revision ID: 20260824_06
Revises: 20260822_05
Create Date: 2026-08-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260824_06"
down_revision: str | None = "20260822_05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATOR_ROLE = "sou2ai_migrator"
RUNTIME_ROLE = "sou2ai_runtime"
OPERATOR_ROLE = "sou2ai_lifecycle_operator"


def upgrade() -> None:
    op.execute(
        "CREATE TYPE public.operational_data_source_status AS ENUM "
        "('CONFIGURED', 'VALIDATED', 'ACTIVE', 'UNHEALTHY', 'DISABLED')"
    )
    op.execute(
        """
        CREATE TABLE public.operational_data_sources (
            id uuid PRIMARY KEY,
            business_id uuid NOT NULL
                REFERENCES public.businesses(id) ON DELETE CASCADE,
            display_name varchar(120) NOT NULL,
            adapter_type varchar(40) NOT NULL,
            connection_profile_key varchar(100) NOT NULL,
            mapping_profile_key varchar(100) NOT NULL,
            mapping_profile_version integer NOT NULL,
            status public.operational_data_source_status NOT NULL
                DEFAULT 'CONFIGURED',
            last_validated_at timestamptz,
            last_successful_health_check_at timestamptz,
            failure_code varchar(100),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_operational_sources_id_business
                UNIQUE (id, business_id),
            CONSTRAINT ck_operational_sources_display_name CHECK (
                char_length(btrim(display_name)) BETWEEN 2 AND 120
            ),
            CONSTRAINT ck_operational_sources_adapter CHECK (
                adapter_type = 'postgresql_readonly'
            ),
            CONSTRAINT ck_operational_sources_connection_profile CHECK (
                connection_profile_key = 'fake_store_postgresql'
            ),
            CONSTRAINT ck_operational_sources_mapping_profile CHECK (
                mapping_profile_key = 'fake_store_minimarket'
                AND mapping_profile_version = 1
            ),
            CONSTRAINT ck_operational_sources_failure_code CHECK (
                failure_code IS NULL OR (
                    char_length(failure_code) BETWEEN 1 AND 100
                    AND failure_code ~ '^[a-z][a-z0-9_]*(\\.[a-z0-9_]+)*$'
                )
            ),
            CONSTRAINT ck_operational_sources_status_metadata CHECK (
                (status = 'CONFIGURED'
                    AND last_validated_at IS NULL
                    AND last_successful_health_check_at IS NULL
                    AND failure_code IS NULL)
                OR (status IN ('VALIDATED', 'ACTIVE')
                    AND last_validated_at IS NOT NULL
                    AND last_successful_health_check_at IS NOT NULL
                    AND failure_code IS NULL)
                OR (status = 'UNHEALTHY'
                    AND last_validated_at IS NOT NULL
                    AND failure_code IS NOT NULL)
                OR (status = 'DISABLED' AND failure_code IS NULL)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_operational_sources_business_created "
        "ON public.operational_data_sources (business_id, created_at, id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_operational_sources_active_type "
        "ON public.operational_data_sources (business_id, adapter_type) "
        "WHERE status = 'ACTIVE'"
    )
    op.execute(
        """
        CREATE FUNCTION public.sou2ai_guard_operational_data_source()
        RETURNS trigger AS $function$
        BEGIN
            IF NEW.business_id IS DISTINCT FROM OLD.business_id
               OR NEW.adapter_type IS DISTINCT FROM OLD.adapter_type
               OR NEW.connection_profile_key IS DISTINCT FROM OLD.connection_profile_key
               OR NEW.mapping_profile_key IS DISTINCT FROM OLD.mapping_profile_key
               OR NEW.mapping_profile_version IS DISTINCT FROM OLD.mapping_profile_version
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'Operational data source scope and profile are immutable.'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
                (OLD.status = 'CONFIGURED' AND NEW.status IN (
                    'VALIDATED', 'UNHEALTHY', 'DISABLED'
                ))
                OR (OLD.status = 'VALIDATED' AND NEW.status IN (
                    'ACTIVE', 'UNHEALTHY', 'DISABLED'
                ))
                OR (OLD.status = 'ACTIVE' AND NEW.status IN (
                    'UNHEALTHY', 'DISABLED'
                ))
                OR (OLD.status = 'UNHEALTHY' AND NEW.status IN (
                    'VALIDATED', 'UNHEALTHY', 'DISABLED'
                ))
                OR (OLD.status = 'DISABLED' AND NEW.status IN (
                    'VALIDATED', 'UNHEALTHY'
                ))
            ) THEN
                RAISE EXCEPTION 'Operational data source transition is not allowed.'
                    USING ERRCODE = '23514';
            END IF;
            NEW.updated_at = pg_catalog.clock_timestamp();
            RETURN NEW;
        END;
        $function$ LANGUAGE plpgsql SET search_path = pg_catalog;

        CREATE TRIGGER trg_operational_data_source_guard
        BEFORE UPDATE ON public.operational_data_sources
        FOR EACH ROW
        EXECUTE FUNCTION public.sou2ai_guard_operational_data_source();
        """
    )
    op.execute(f"ALTER TABLE public.operational_data_sources OWNER TO {MIGRATOR_ROLE}")
    op.execute(
        f"REVOKE ALL ON TABLE public.operational_data_sources "
        f"FROM PUBLIC, {OPERATOR_ROLE}"
    )
    op.execute(
        f"GRANT SELECT, INSERT ON TABLE public.operational_data_sources "
        f"TO {RUNTIME_ROLE}"
    )
    op.execute(
        "GRANT UPDATE (display_name, status, last_validated_at, "
        "last_successful_health_check_at, failure_code) "
        f"ON TABLE public.operational_data_sources TO {RUNTIME_ROLE}"
    )
    op.execute(
        f"ALTER TYPE public.operational_data_source_status OWNER TO {MIGRATOR_ROLE}"
    )
    op.execute(
        f"GRANT USAGE ON TYPE public.operational_data_source_status TO {RUNTIME_ROLE}"
    )
    op.execute(
        "ALTER FUNCTION public.sou2ai_guard_operational_data_source() "
        f"OWNER TO {MIGRATOR_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.sou2ai_guard_operational_data_source() "
        f"FROM PUBLIC, {RUNTIME_ROLE}, {OPERATOR_ROLE}"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_operational_data_source_guard "
        "ON public.operational_data_sources"
    )
    op.execute("DROP FUNCTION IF EXISTS public.sou2ai_guard_operational_data_source()")
    op.execute("DROP TABLE IF EXISTS public.operational_data_sources")
    op.execute("DROP TYPE IF EXISTS public.operational_data_source_status")
