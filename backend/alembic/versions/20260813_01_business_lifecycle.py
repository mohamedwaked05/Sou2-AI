"""Implement authoritative business lifecycle management.

Revision ID: 20260813_01
Revises: 20260812_03
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260813_01"
down_revision: str | None = "20260812_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

business_status = postgresql.ENUM(
    "PENDING",
    "ACTIVE",
    "DISABLED",
    name="business_status",
    create_type=False,
)


def _drop_legacy_active_guards() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_businesses_active_profile_fields ON businesses"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_opening_shifts_active_profile "
        "ON business_opening_shifts"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_opening_days_active_profile "
        "ON business_opening_days"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_businesses_activation ON businesses")
    op.execute("DROP FUNCTION IF EXISTS sou2ai_guard_active_profile_fields()")
    op.execute("DROP FUNCTION IF EXISTS sou2ai_guard_active_schedule()")
    op.execute("DROP FUNCTION IF EXISTS sou2ai_guard_business_activation()")


def _create_status_profile_guards() -> None:
    op.execute(
        """
        CREATE FUNCTION sou2ai_guard_active_profile_fields() RETURNS trigger AS $$
        BEGIN
            IF NEW.status = 'ACTIVE'
               AND NOT sou2ai_business_profile_complete(NEW.id) THEN
                RAISE EXCEPTION 'An active business must retain a valid profile.'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER trg_businesses_active_profile_fields
        AFTER UPDATE OF name, description, category, custom_category,
                        governorate, district, city, address_line ON businesses
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION sou2ai_guard_active_profile_fields();

        CREATE FUNCTION sou2ai_guard_active_schedule() RETURNS trigger AS $$
        DECLARE target_business uuid;
        BEGIN
            IF TG_TABLE_NAME = 'business_opening_days' THEN
                target_business := COALESCE(NEW.business_id, OLD.business_id);
            ELSE
                SELECT business_id INTO target_business FROM business_opening_days
                WHERE id = COALESCE(NEW.opening_day_id, OLD.opening_day_id);
            END IF;
            IF target_business IS NOT NULL
               AND EXISTS (SELECT 1 FROM businesses
                           WHERE id = target_business AND status = 'ACTIVE')
               AND NOT sou2ai_business_profile_complete(target_business) THEN
                RAISE EXCEPTION 'An active business must retain a valid profile.'
                    USING ERRCODE = '23514';
            END IF;
            RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER trg_opening_days_active_profile
        AFTER INSERT OR UPDATE OR DELETE ON business_opening_days
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION sou2ai_guard_active_schedule();

        CREATE CONSTRAINT TRIGGER trg_opening_shifts_active_profile
        AFTER INSERT OR UPDATE OR DELETE ON business_opening_shifts
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION sou2ai_guard_active_schedule();
        """
    )


def _create_lifecycle_controls() -> None:
    op.execute(
        """
        CREATE FUNCTION sou2ai_guard_business_initial_status() RETURNS trigger AS $$
        BEGIN
            IF NEW.status <> 'PENDING' THEN
                RAISE EXCEPTION 'New businesses must begin in PENDING status.'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_businesses_initial_status
        BEFORE INSERT ON businesses
        FOR EACH ROW EXECUTE FUNCTION sou2ai_guard_business_initial_status();

        CREATE FUNCTION sou2ai_guard_business_status_update() RETURNS trigger AS $$
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
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_businesses_lifecycle_status
        BEFORE UPDATE OF status ON businesses
        FOR EACH ROW EXECUTE FUNCTION sou2ai_guard_business_status_update();

        CREATE FUNCTION sou2ai_guard_lifecycle_history() RETURNS trigger AS $$
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
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_business_lifecycle_history_append_only
        BEFORE INSERT OR UPDATE OR DELETE ON business_lifecycle_history
        FOR EACH ROW EXECUTE FUNCTION sou2ai_guard_lifecycle_history();

        CREATE TRIGGER trg_business_lifecycle_history_no_truncate
        BEFORE TRUNCATE ON business_lifecycle_history
        FOR EACH STATEMENT EXECUTE FUNCTION sou2ai_guard_lifecycle_history();

        CREATE FUNCTION public.sou2ai_change_business_status(
            target_business_id uuid,
            requested_status business_status,
            admin_identifier text,
            reason text
        ) RETURNS TABLE(business_id uuid, status business_status) AS $$
        DECLARE
            previous_status business_status;
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
                RAISE EXCEPTION 'Business status must change.' USING ERRCODE = '23514';
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
                gen_random_uuid(),
                target_business_id,
                previous_status,
                requested_status,
                clean_admin_identifier,
                clean_reason
            );

            PERFORM set_config('sou2ai.lifecycle_business_id', '', true);
            PERFORM set_config('sou2ai.lifecycle_previous_status', '', true);
            PERFORM set_config('sou2ai.lifecycle_new_status', '', true);

            RETURN QUERY SELECT target_business_id, requested_status;
        EXCEPTION WHEN OTHERS THEN
            PERFORM set_config('sou2ai.lifecycle_business_id', '', true);
            PERFORM set_config('sou2ai.lifecycle_previous_status', '', true);
            PERFORM set_config('sou2ai.lifecycle_new_status', '', true);
            RAISE;
        END;
        $$ LANGUAGE plpgsql
           SECURITY DEFINER
           SET search_path = public, pg_temp;

        REVOKE ALL ON FUNCTION public.sou2ai_change_business_status(
            uuid, business_status, text, text
        ) FROM PUBLIC;
        """
    )


def _create_legacy_boolean_guards() -> None:
    op.execute(
        """
        CREATE FUNCTION sou2ai_guard_business_activation() RETURNS trigger AS $$
        BEGIN
            IF NOT OLD.is_active AND NEW.is_active
               AND NOT sou2ai_business_profile_complete(NEW.id) THEN
                RAISE EXCEPTION 'Business profile must be complete before activation.'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_businesses_activation
        AFTER UPDATE OF is_active ON businesses
        FOR EACH ROW EXECUTE FUNCTION sou2ai_guard_business_activation();

        CREATE FUNCTION sou2ai_guard_active_profile_fields() RETURNS trigger AS $$
        BEGIN
            IF NEW.is_active
               AND NOT sou2ai_business_profile_complete(NEW.id) THEN
                RAISE EXCEPTION 'An active business must retain a valid profile.'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER trg_businesses_active_profile_fields
        AFTER UPDATE OF name, description, category, custom_category,
                        governorate, district, city, address_line ON businesses
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION sou2ai_guard_active_profile_fields();

        CREATE FUNCTION sou2ai_guard_active_schedule() RETURNS trigger AS $$
        DECLARE target_business uuid;
        BEGIN
            IF TG_TABLE_NAME = 'business_opening_days' THEN
                target_business := COALESCE(NEW.business_id, OLD.business_id);
            ELSE
                SELECT business_id INTO target_business FROM business_opening_days
                WHERE id = COALESCE(NEW.opening_day_id, OLD.opening_day_id);
            END IF;
            IF target_business IS NOT NULL
               AND EXISTS (SELECT 1 FROM businesses
                           WHERE id = target_business AND is_active)
               AND NOT sou2ai_business_profile_complete(target_business) THEN
                RAISE EXCEPTION 'An active business must retain a valid profile.'
                    USING ERRCODE = '23514';
            END IF;
            RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER trg_opening_days_active_profile
        AFTER INSERT OR UPDATE OR DELETE ON business_opening_days
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION sou2ai_guard_active_schedule();

        CREATE CONSTRAINT TRIGGER trg_opening_shifts_active_profile
        AFTER INSERT OR UPDATE OR DELETE ON business_opening_shifts
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION sou2ai_guard_active_schedule();
        """
    )


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE business_status ADD VALUE IF NOT EXISTS 'ACTIVE'")
        op.execute("ALTER TYPE business_status ADD VALUE IF NOT EXISTS 'DISABLED'")

    op.create_table(
        "business_lifecycle_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("previous_status", business_status, nullable=False),
        sa.Column("new_status", business_status, nullable=False),
        sa.Column("admin_identifier", sa.String(length=320), nullable=False),
        sa.Column("reason", sa.String(length=2000), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "previous_status <> new_status",
            name="ck_business_lifecycle_history_status_changed",
        ),
        sa.CheckConstraint(
            "char_length(btrim(admin_identifier)) BETWEEN 1 AND 320",
            name="ck_business_lifecycle_history_admin_length",
        ),
        sa.CheckConstraint(
            "char_length(btrim(reason)) BETWEEN 1 AND 2000",
            name="ck_business_lifecycle_history_reason_length",
        ),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_business_lifecycle_history_business_changed",
        "business_lifecycle_history",
        ["business_id", "changed_at"],
    )

    op.execute(
        """
        UPDATE businesses
        SET status = CASE
            WHEN is_active THEN 'ACTIVE'::business_status
            ELSE 'PENDING'::business_status
        END;

        INSERT INTO business_lifecycle_history (
            id,
            business_id,
            previous_status,
            new_status,
            admin_identifier,
            reason
        )
        SELECT
            gen_random_uuid(),
            id,
            'PENDING'::business_status,
            'ACTIVE'::business_status,
            'system:migration',
            'Migrated from legacy active state'
        FROM businesses
        WHERE is_active;
        """
    )

    _drop_legacy_active_guards()
    op.drop_column("businesses", "is_active")
    _create_status_profile_guards()
    _create_lifecycle_controls()


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.sou2ai_change_business_status("
        "uuid, business_status, text, text)"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_business_lifecycle_history_append_only "
        "ON business_lifecycle_history"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_business_lifecycle_history_no_truncate "
        "ON business_lifecycle_history"
    )
    op.execute("DROP FUNCTION IF EXISTS sou2ai_guard_lifecycle_history()")
    op.execute("DROP TRIGGER IF EXISTS trg_businesses_lifecycle_status ON businesses")
    op.execute("DROP FUNCTION IF EXISTS sou2ai_guard_business_status_update()")
    op.execute("DROP TRIGGER IF EXISTS trg_businesses_initial_status ON businesses")
    op.execute("DROP FUNCTION IF EXISTS sou2ai_guard_business_initial_status()")
    _drop_legacy_active_guards()

    op.add_column(
        "businesses",
        sa.Column("is_active", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.execute("UPDATE businesses SET is_active = (status = 'ACTIVE')")

    op.drop_index(
        "ix_business_lifecycle_history_business_changed",
        table_name="business_lifecycle_history",
    )
    op.drop_table("business_lifecycle_history")

    op.alter_column("businesses", "status", server_default=None)
    op.execute("UPDATE businesses SET status = 'PENDING'")
    op.execute(
        "ALTER TABLE businesses ALTER COLUMN status TYPE text USING status::text"
    )
    op.execute("DROP TYPE business_status")
    op.execute("CREATE TYPE business_status AS ENUM ('PENDING')")
    op.execute(
        "ALTER TABLE businesses ALTER COLUMN status TYPE business_status "
        "USING status::business_status"
    )
    op.alter_column(
        "businesses",
        "status",
        server_default=sa.text("'PENDING'::business_status"),
    )
    _create_legacy_boolean_guards()
