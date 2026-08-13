"""Add persistent API limits and per-business AI budgets.

Revision ID: 20260813_03
Revises: 20260813_02
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260813_03"
down_revision: str | None = "20260813_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATOR_ROLE = "sou2ai_migrator"
RUNTIME_ROLE = "sou2ai_runtime"
OPERATOR_ROLE = "sou2ai_lifecycle_operator"


def _create_rate_limit_tables() -> None:
    op.create_table(
        "registration_rate_limit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("normalized_email", sa.String(length=320), nullable=False),
        sa.Column("client_ip", sa.String(length=45), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_registration_rate_email_created",
        "registration_rate_limit_events",
        ["normalized_email", "created_at"],
    )
    op.create_index(
        "ix_registration_rate_ip_created",
        "registration_rate_limit_events",
        ["client_ip", "created_at"],
    )
    op.create_index(
        "ix_registration_rate_created",
        "registration_rate_limit_events",
        ["created_at"],
    )

    op.create_table(
        "owner_chat_rate_limit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation_attempt", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "generation_attempt > 0", name="ck_owner_chat_rate_attempt_positive"
        ),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["owner_message_id"], ["owner_chat_messages.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_message_id",
            "generation_attempt",
            name="uq_owner_chat_rate_message_attempt",
        ),
    )
    op.create_index(
        "ix_owner_chat_rate_business_created",
        "owner_chat_rate_limit_events",
        ["business_id", "created_at"],
    )
    op.create_index(
        "ix_owner_chat_rate_created",
        "owner_chat_rate_limit_events",
        ["created_at"],
    )


def _create_budget_tables() -> None:
    op.create_table(
        "business_ai_allowance_configs",
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "daily_token_allowance",
            sa.Integer(),
            server_default="20000",
            nullable=False,
        ),
        sa.Column(
            "owner_reserve_percent", sa.Integer(), server_default="25", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "daily_token_allowance BETWEEN 1 AND 1000000000",
            name="ck_ai_allowance_daily_range",
        ),
        sa.CheckConstraint(
            "owner_reserve_percent BETWEEN 0 AND 100",
            name="ck_ai_allowance_reserve_range",
        ),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("business_id"),
    )

    op.create_table(
        "business_ai_allowance_audit",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("previous_daily_token_allowance", sa.Integer(), nullable=False),
        sa.Column("new_daily_token_allowance", sa.Integer(), nullable=False),
        sa.Column("previous_owner_reserve_percent", sa.Integer(), nullable=False),
        sa.Column("new_owner_reserve_percent", sa.Integer(), nullable=False),
        sa.Column("admin_identifier", sa.String(length=320), nullable=False),
        sa.Column("reason", sa.String(length=2000), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "previous_daily_token_allowance <> new_daily_token_allowance OR "
            "previous_owner_reserve_percent <> new_owner_reserve_percent",
            name="ck_ai_allowance_audit_changed",
        ),
        sa.CheckConstraint(
            "char_length(btrim(admin_identifier)) BETWEEN 1 AND 320",
            name="ck_ai_allowance_audit_admin_length",
        ),
        sa.CheckConstraint(
            "char_length(btrim(reason)) BETWEEN 1 AND 2000",
            name="ck_ai_allowance_audit_reason_length",
        ),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_allowance_audit_business_changed",
        "business_ai_allowance_audit",
        ["business_id", "changed_at"],
    )

    op.create_table(
        "business_ai_usage_daily",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "input_tokens_used", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "output_tokens_used", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "total_tokens_used", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("tokens_reserved", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "window_end > window_start", name="ck_ai_usage_daily_window_order"
        ),
        sa.CheckConstraint(
            "input_tokens_used >= 0 AND output_tokens_used >= 0 AND "
            "total_tokens_used >= 0 AND tokens_reserved >= 0",
            name="ck_ai_usage_daily_nonnegative",
        ),
        sa.CheckConstraint(
            "total_tokens_used = input_tokens_used + output_tokens_used",
            name="ck_ai_usage_daily_total",
        ),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "business_id", "window_start", name="uq_ai_usage_daily_window"
        ),
    )
    op.create_index(
        "ix_ai_usage_daily_business_end",
        "business_ai_usage_daily",
        ["business_id", "window_end"],
    )
    op.create_index(
        "ix_ai_usage_daily_window_end", "business_ai_usage_daily", ["window_end"]
    )

    op.create_table(
        "ai_usage_reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("owner_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("generation_attempt", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("capability", sa.String(length=50), nullable=False),
        sa.Column("estimated_input_tokens", sa.Integer(), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("counts_authoritative", sa.Boolean(), nullable=True),
        sa.Column("provider_identifier", sa.String(length=50), nullable=True),
        sa.Column("model_identifier", sa.String(length=100), nullable=True),
        sa.Column(
            "status", sa.String(length=20), server_default="reserved", nullable=False
        ),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "channel IN ('owner', 'customer', 'whatsapp')",
            name="ck_ai_reservation_channel",
        ),
        sa.CheckConstraint(
            "status IN ('reserved', 'completed', 'released', 'charged')",
            name="ck_ai_reservation_status",
        ),
        sa.CheckConstraint(
            "estimated_input_tokens >= 0 AND max_output_tokens > 0 AND "
            "reserved_tokens = estimated_input_tokens + max_output_tokens",
            name="ck_ai_reservation_reserved_total",
        ),
        sa.CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name="ck_ai_reservation_input_nonnegative",
        ),
        sa.CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name="ck_ai_reservation_output_nonnegative",
        ),
        sa.CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0",
            name="ck_ai_reservation_total_nonnegative",
        ),
        sa.CheckConstraint(
            "total_tokens IS NULL OR total_tokens = input_tokens + output_tokens",
            name="ck_ai_reservation_actual_total",
        ),
        sa.CheckConstraint(
            "window_end > window_start AND lease_expires_at > created_at",
            name="ck_ai_reservation_time_order",
        ),
        sa.CheckConstraint(
            "char_length(capability) BETWEEN 1 AND 50",
            name="ck_ai_reservation_capability_length",
        ),
        sa.CheckConstraint(
            "provider_identifier IS NULL OR char_length(provider_identifier) <= 50",
            name="ck_ai_reservation_provider_length",
        ),
        sa.CheckConstraint(
            "model_identifier IS NULL OR char_length(model_identifier) <= 100",
            name="ck_ai_reservation_model_length",
        ),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["owner_message_id"], ["owner_chat_messages.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_message_id",
            "generation_attempt",
            name="uq_ai_reservation_message_attempt",
        ),
    )
    op.create_index(
        "ix_ai_reservation_business_window",
        "ai_usage_reservations",
        ["business_id", "window_start"],
    )
    op.create_index(
        "ix_ai_reservation_lease",
        "ai_usage_reservations",
        ["status", "lease_expires_at"],
    )
    op.create_index(
        "ix_ai_reservation_created", "ai_usage_reservations", ["created_at"]
    )


def _install_allowance_defaults_and_audit_guards() -> None:
    op.execute(
        """
        INSERT INTO public.business_ai_allowance_configs (business_id)
        SELECT business.id FROM public.businesses AS business
        ON CONFLICT (business_id) DO NOTHING;

        CREATE FUNCTION public.sou2ai_create_default_ai_allowance()
        RETURNS trigger AS $function$
        BEGIN
            INSERT INTO public.business_ai_allowance_configs (business_id)
            VALUES (NEW.id);
            RETURN NEW;
        END;
        $function$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog;

        CREATE TRIGGER trg_businesses_default_ai_allowance
        AFTER INSERT ON public.businesses
        FOR EACH ROW EXECUTE FUNCTION public.sou2ai_create_default_ai_allowance();

        CREATE FUNCTION public.sou2ai_guard_ai_allowance_audit_append_only()
        RETURNS trigger AS $function$
        BEGIN
            RAISE EXCEPTION 'Business AI allowance audit is append-only.'
                USING ERRCODE = '55000';
        END;
        $function$ LANGUAGE plpgsql SET search_path = pg_catalog;

        CREATE TRIGGER trg_ai_allowance_audit_append_only
        BEFORE UPDATE OR DELETE ON public.business_ai_allowance_audit
        FOR EACH ROW
        EXECUTE FUNCTION public.sou2ai_guard_ai_allowance_audit_append_only();

        CREATE TRIGGER trg_ai_allowance_audit_no_truncate
        BEFORE TRUNCATE ON public.business_ai_allowance_audit
        FOR EACH STATEMENT
        EXECUTE FUNCTION public.sou2ai_guard_ai_allowance_audit_append_only();
        """
    )


def _create_budget_functions() -> None:
    op.execute(
        """
        CREATE FUNCTION public.sou2ai_charge_expired_ai_reservations(
            target_business_id uuid,
            target_window_start timestamptz
        ) RETURNS integer AS $function$
        DECLARE
            expired_record record;
            charged_count integer := 0;
        BEGIN
            FOR expired_record IN
                SELECT reservation.id,
                       reservation.reserved_tokens,
                       reservation.estimated_input_tokens,
                       reservation.max_output_tokens
                FROM public.ai_usage_reservations AS reservation
                WHERE reservation.business_id = target_business_id
                  AND reservation.window_start = target_window_start
                  AND reservation.status = 'reserved'
                  AND reservation.lease_expires_at <= pg_catalog.clock_timestamp()
                FOR UPDATE
            LOOP
                UPDATE public.ai_usage_reservations
                SET status = 'charged',
                    input_tokens = expired_record.estimated_input_tokens,
                    output_tokens = expired_record.max_output_tokens,
                    total_tokens = expired_record.reserved_tokens,
                    counts_authoritative = false,
                    reconciled_at = pg_catalog.clock_timestamp()
                WHERE id = expired_record.id;

                UPDATE public.business_ai_usage_daily
                SET input_tokens_used = input_tokens_used
                        + expired_record.estimated_input_tokens,
                    output_tokens_used = output_tokens_used
                        + expired_record.max_output_tokens,
                    total_tokens_used = total_tokens_used
                        + expired_record.reserved_tokens,
                    tokens_reserved = tokens_reserved
                        - expired_record.reserved_tokens,
                    updated_at = pg_catalog.clock_timestamp()
                WHERE business_id = target_business_id
                  AND window_start = target_window_start;
                charged_count := charged_count + 1;
            END LOOP;
            RETURN charged_count;
        END;
        $function$ LANGUAGE plpgsql SET search_path = pg_catalog;

        CREATE FUNCTION public.sou2ai_reserve_ai_usage(
            target_business_id uuid,
            target_user_id uuid,
            target_owner_message_id uuid,
            target_generation_attempt integer,
            target_channel text,
            target_capability text,
            target_estimated_input_tokens integer,
            target_max_output_tokens integer,
            target_lease_seconds integer
        ) RETURNS TABLE(
            reservation_id uuid,
            reserved_tokens integer,
            reset_at timestamptz
        ) AS $function$
        DECLARE
            allowance integer;
            reserve_percent integer;
            effective_limit integer;
            requested_tokens integer;
            usage_record record;
            new_reservation_id uuid := pg_catalog.gen_random_uuid();
            business_timezone text;
            local_day date;
            target_window_start timestamptz;
            target_window_end timestamptz;
        BEGIN
            IF target_channel NOT IN ('owner', 'customer', 'whatsapp')
               OR pg_catalog.btrim(target_capability) = ''
               OR pg_catalog.char_length(pg_catalog.btrim(target_capability)) > 50
               OR target_estimated_input_tokens < 0
               OR target_max_output_tokens < 1
               OR target_generation_attempt < 1
               OR target_lease_seconds < 1 THEN
                RAISE EXCEPTION 'Invalid AI usage reservation.'
                    USING ERRCODE = '22023';
            END IF;

            SELECT config.daily_token_allowance, config.owner_reserve_percent,
                   business.timezone
            INTO allowance, reserve_percent, business_timezone
            FROM public.business_ai_allowance_configs AS config
            JOIN public.businesses AS business
              ON business.id = config.business_id
            WHERE config.business_id = target_business_id
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Business AI allowance was not found.'
                    USING ERRCODE = 'P0002';
            END IF;

            local_day := pg_catalog.timezone(
                business_timezone, pg_catalog.clock_timestamp()
            )::date;
            target_window_start := pg_catalog.timezone(
                business_timezone, local_day::timestamp
            );
            target_window_end := pg_catalog.timezone(
                business_timezone, (local_day + 1)::timestamp
            );

            INSERT INTO public.business_ai_usage_daily (
                id, business_id, window_start, window_end
            ) VALUES (
                pg_catalog.gen_random_uuid(), target_business_id,
                target_window_start, target_window_end
            ) ON CONFLICT (business_id, window_start) DO NOTHING;

            PERFORM public.sou2ai_charge_expired_ai_reservations(
                target_business_id, target_window_start
            );

            SELECT daily.total_tokens_used, daily.tokens_reserved
            INTO usage_record
            FROM public.business_ai_usage_daily AS daily
            WHERE daily.business_id = target_business_id
              AND daily.window_start = target_window_start
            FOR UPDATE;

            requested_tokens := target_estimated_input_tokens
                + target_max_output_tokens;
            effective_limit := CASE
                WHEN target_channel = 'owner' THEN allowance
                ELSE allowance - ((allowance * reserve_percent) / 100)
            END;
            IF usage_record.total_tokens_used + usage_record.tokens_reserved
                    + requested_tokens > effective_limit THEN
                RAISE EXCEPTION 'daily_ai_token_limit_reached'
                    USING ERRCODE = 'P0001';
            END IF;

            INSERT INTO public.ai_usage_reservations (
                id, business_id, user_id, owner_message_id, generation_attempt,
                channel, capability, estimated_input_tokens, max_output_tokens,
                reserved_tokens, window_start, window_end, lease_expires_at
            ) VALUES (
                new_reservation_id, target_business_id, target_user_id,
                target_owner_message_id, target_generation_attempt,
                target_channel, pg_catalog.btrim(target_capability),
                target_estimated_input_tokens, target_max_output_tokens,
                requested_tokens, target_window_start, target_window_end,
                pg_catalog.clock_timestamp()
                    + pg_catalog.make_interval(secs => target_lease_seconds)
            );
            UPDATE public.business_ai_usage_daily
            SET tokens_reserved = tokens_reserved + requested_tokens,
                updated_at = pg_catalog.clock_timestamp()
            WHERE business_id = target_business_id
              AND window_start = target_window_start;

            RETURN QUERY SELECT new_reservation_id, requested_tokens,
                                target_window_end;
        END;
        $function$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog;

        CREATE FUNCTION public.sou2ai_reconcile_ai_usage(
            target_reservation_id uuid,
            target_input_tokens integer,
            target_output_tokens integer,
            target_counts_authoritative boolean,
            target_provider_identifier text,
            target_model_identifier text,
            target_outcome text
        ) RETURNS TABLE(
            charged_tokens integer,
            reconciled boolean
        ) AS $function$
        DECLARE
            reservation_record record;
            final_input integer;
            final_output integer;
            final_total integer;
            final_status text;
        BEGIN
            SELECT reservation.* INTO reservation_record
            FROM public.ai_usage_reservations AS reservation
            WHERE reservation.id = target_reservation_id
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'AI usage reservation was not found.'
                    USING ERRCODE = 'P0002';
            END IF;
            IF reservation_record.status <> 'reserved' THEN
                RETURN QUERY SELECT
                    pg_catalog.coalesce(reservation_record.total_tokens, 0), false;
                RETURN;
            END IF;
            IF target_outcome NOT IN (
                'completed', 'reported_failure', 'release', 'uncertain'
            ) THEN
                RAISE EXCEPTION 'Invalid AI usage outcome.' USING ERRCODE = '22023';
            END IF;

            IF target_outcome = 'release' THEN
                final_input := 0;
                final_output := 0;
                final_status := 'released';
            ELSIF target_outcome = 'uncertain' THEN
                final_input := reservation_record.estimated_input_tokens;
                final_output := reservation_record.max_output_tokens;
                target_counts_authoritative := false;
                final_status := 'charged';
            ELSE
                IF target_input_tokens IS NULL OR target_input_tokens < 0
                   OR target_output_tokens IS NULL OR target_output_tokens < 0 THEN
                    RAISE EXCEPTION 'Token counts must be nonnegative.'
                        USING ERRCODE = '22023';
                END IF;
                final_input := target_input_tokens;
                final_output := target_output_tokens;
                final_status := CASE WHEN target_outcome = 'completed'
                                     THEN 'completed' ELSE 'charged' END;
            END IF;
            final_total := final_input + final_output;

            UPDATE public.business_ai_usage_daily
            SET input_tokens_used = input_tokens_used + final_input,
                output_tokens_used = output_tokens_used + final_output,
                total_tokens_used = total_tokens_used + final_total,
                tokens_reserved = tokens_reserved
                    - reservation_record.reserved_tokens,
                updated_at = pg_catalog.clock_timestamp()
            WHERE business_id = reservation_record.business_id
              AND window_start = reservation_record.window_start;

            UPDATE public.ai_usage_reservations
            SET input_tokens = final_input,
                output_tokens = final_output,
                total_tokens = final_total,
                counts_authoritative = target_counts_authoritative,
                provider_identifier = CASE
                    WHEN target_provider_identifier IS NULL
                      OR pg_catalog.btrim(target_provider_identifier) = '' THEN NULL
                    ELSE pg_catalog.left(
                        pg_catalog.btrim(target_provider_identifier), 50
                    )
                END,
                model_identifier = CASE
                    WHEN target_model_identifier IS NULL
                      OR pg_catalog.btrim(target_model_identifier) = '' THEN NULL
                    ELSE pg_catalog.left(
                        pg_catalog.btrim(target_model_identifier), 100
                    )
                END,
                status = final_status,
                reconciled_at = pg_catalog.clock_timestamp()
            WHERE id = target_reservation_id;
            RETURN QUERY SELECT final_total, true;
        END;
        $function$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog;

        CREATE FUNCTION public.sou2ai_get_current_ai_usage(
            target_business_id uuid
        ) RETURNS TABLE(
            usage_window_start timestamptz,
            usage_window_end timestamptz,
            daily_token_allowance integer,
            owner_reserve_percent integer,
            input_tokens_used integer,
            output_tokens_used integer,
            total_tokens_used integer,
            tokens_reserved integer
        ) AS $function$
        DECLARE
            business_timezone text;
            local_day date;
            target_window_start timestamptz;
            target_window_end timestamptz;
        BEGIN
            SELECT business.timezone INTO business_timezone
            FROM public.business_ai_allowance_configs AS config
            JOIN public.businesses AS business
              ON business.id = config.business_id
            WHERE config.business_id = target_business_id FOR UPDATE OF config;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Business AI allowance was not found.'
                    USING ERRCODE = 'P0002';
            END IF;
            local_day := pg_catalog.timezone(
                business_timezone, pg_catalog.clock_timestamp()
            )::date;
            target_window_start := pg_catalog.timezone(
                business_timezone, local_day::timestamp
            );
            target_window_end := pg_catalog.timezone(
                business_timezone, (local_day + 1)::timestamp
            );
            INSERT INTO public.business_ai_usage_daily (
                id, business_id, window_start, window_end
            ) VALUES (
                pg_catalog.gen_random_uuid(), target_business_id,
                target_window_start, target_window_end
            ) ON CONFLICT (business_id, window_start) DO NOTHING;
            PERFORM public.sou2ai_charge_expired_ai_reservations(
                target_business_id, target_window_start
            );
            RETURN QUERY
            SELECT target_window_start,
                   target_window_end,
                   config.daily_token_allowance,
                   config.owner_reserve_percent,
                   daily.input_tokens_used,
                   daily.output_tokens_used,
                   daily.total_tokens_used,
                   daily.tokens_reserved
            FROM public.business_ai_allowance_configs AS config
            JOIN public.business_ai_usage_daily AS daily
              ON daily.business_id = config.business_id
             AND daily.window_start = target_window_start
            WHERE config.business_id = target_business_id;
        END;
        $function$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog;
        """
    )


def _create_allowance_admin_function() -> None:
    op.execute(
        """
        CREATE FUNCTION public.sou2ai_change_business_ai_allowance(
            target_business_id uuid,
            new_daily_token_allowance integer,
            new_owner_reserve_percent integer,
            admin_identifier text,
            reason text
        ) RETURNS TABLE(
            business_id uuid,
            daily_token_allowance integer,
            owner_reserve_percent integer
        ) AS $function$
        DECLARE
            previous_record record;
            clean_admin text := pg_catalog.btrim(admin_identifier);
            clean_reason text := pg_catalog.btrim(reason);
        BEGIN
            IF new_daily_token_allowance NOT BETWEEN 1 AND 1000000000
               OR new_owner_reserve_percent NOT BETWEEN 0 AND 100 THEN
                RAISE EXCEPTION 'AI allowance configuration is invalid.'
                    USING ERRCODE = '22023';
            END IF;
            IF clean_admin IS NULL
               OR pg_catalog.char_length(clean_admin) NOT BETWEEN 1 AND 320 THEN
                RAISE EXCEPTION
                    'Admin identifier must contain between 1 and 320 characters.'
                    USING ERRCODE = '22023';
            END IF;
            IF clean_reason IS NULL
               OR pg_catalog.char_length(clean_reason) NOT BETWEEN 1 AND 2000 THEN
                RAISE EXCEPTION 'Reason must contain between 1 and 2000 characters.'
                    USING ERRCODE = '22023';
            END IF;

            PERFORM 1 FROM public.businesses AS business
            WHERE business.id = target_business_id FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Business was not found.' USING ERRCODE = 'P0002';
            END IF;
            SELECT config.* INTO previous_record
            FROM public.business_ai_allowance_configs AS config
            WHERE config.business_id = target_business_id FOR UPDATE;
            IF previous_record.daily_token_allowance = new_daily_token_allowance
               AND previous_record.owner_reserve_percent = new_owner_reserve_percent
            THEN
                RAISE EXCEPTION 'AI allowance configuration must change.'
                    USING ERRCODE = '23514';
            END IF;

            UPDATE public.business_ai_allowance_configs
            SET daily_token_allowance = new_daily_token_allowance,
                owner_reserve_percent = new_owner_reserve_percent,
                updated_at = pg_catalog.clock_timestamp()
            WHERE business_ai_allowance_configs.business_id = target_business_id;
            INSERT INTO public.business_ai_allowance_audit (
                id, business_id, previous_daily_token_allowance,
                new_daily_token_allowance, previous_owner_reserve_percent,
                new_owner_reserve_percent, admin_identifier, reason
            ) VALUES (
                pg_catalog.gen_random_uuid(), target_business_id,
                previous_record.daily_token_allowance, new_daily_token_allowance,
                previous_record.owner_reserve_percent, new_owner_reserve_percent,
                clean_admin, clean_reason
            );
            RETURN QUERY SELECT target_business_id, new_daily_token_allowance,
                                new_owner_reserve_percent;
        END;
        $function$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog;
        """
    )


def _create_retention_function() -> None:
    op.execute(
        """
        CREATE FUNCTION public.sou2ai_cleanup_security_records(
            target_now timestamptz,
            target_batch_size integer
        ) RETURNS TABLE(
            owner_events_deleted integer,
            registration_events_deleted integer,
            reservations_deleted integer,
            summaries_deleted integer
        ) AS $function$
        DECLARE
            target record;
        BEGIN
            IF target_now IS NULL OR target_batch_size < 1 THEN
                RAISE EXCEPTION 'Invalid security retention request.'
                    USING ERRCODE = '22023';
            END IF;
            FOR target IN
                SELECT DISTINCT reservation.business_id, reservation.window_start
                FROM public.ai_usage_reservations AS reservation
                WHERE reservation.status = 'reserved'
                  AND reservation.lease_expires_at <= target_now
            LOOP
                PERFORM public.sou2ai_charge_expired_ai_reservations(
                    target.business_id, target.window_start
                );
            END LOOP;

            WITH expired AS (
                SELECT event.id FROM public.owner_chat_rate_limit_events AS event
                WHERE event.created_at < target_now - interval '24 hours'
                ORDER BY event.created_at, event.id LIMIT target_batch_size
                FOR UPDATE SKIP LOCKED
            )
            DELETE FROM public.owner_chat_rate_limit_events AS event
            USING expired WHERE event.id = expired.id;
            GET DIAGNOSTICS owner_events_deleted = ROW_COUNT;

            WITH expired AS (
                SELECT event.id FROM public.registration_rate_limit_events AS event
                WHERE event.created_at < target_now - interval '48 hours'
                ORDER BY event.created_at, event.id LIMIT target_batch_size
                FOR UPDATE SKIP LOCKED
            )
            DELETE FROM public.registration_rate_limit_events AS event
            USING expired WHERE event.id = expired.id;
            GET DIAGNOSTICS registration_events_deleted = ROW_COUNT;

            WITH expired AS (
                SELECT reservation.id
                FROM public.ai_usage_reservations AS reservation
                WHERE reservation.created_at < target_now - interval '90 days'
                  AND reservation.status <> 'reserved'
                ORDER BY reservation.created_at, reservation.id
                LIMIT target_batch_size FOR UPDATE SKIP LOCKED
            )
            DELETE FROM public.ai_usage_reservations AS reservation
            USING expired WHERE reservation.id = expired.id;
            GET DIAGNOSTICS reservations_deleted = ROW_COUNT;

            WITH expired AS (
                SELECT summary.id FROM public.business_ai_usage_daily AS summary
                WHERE summary.window_end < target_now - interval '12 months'
                ORDER BY summary.window_end, summary.id LIMIT target_batch_size
                FOR UPDATE SKIP LOCKED
            )
            DELETE FROM public.business_ai_usage_daily AS summary
            USING expired WHERE summary.id = expired.id;
            GET DIAGNOSTICS summaries_deleted = ROW_COUNT;
            RETURN NEXT;
        END;
        $function$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog;
        """
    )


def _secure_objects() -> None:
    protected_tables = (
        "business_ai_allowance_configs",
        "business_ai_allowance_audit",
        "business_ai_usage_daily",
        "ai_usage_reservations",
    )
    rate_tables = (
        "registration_rate_limit_events",
        "owner_chat_rate_limit_events",
    )
    functions = (
        "public.sou2ai_create_default_ai_allowance()",
        "public.sou2ai_guard_ai_allowance_audit_append_only()",
        "public.sou2ai_charge_expired_ai_reservations(uuid, timestamptz)",
        "public.sou2ai_reserve_ai_usage(uuid, uuid, uuid, integer, text, text, integer, integer, integer)",
        "public.sou2ai_reconcile_ai_usage(uuid, integer, integer, boolean, text, text, text)",
        "public.sou2ai_get_current_ai_usage(uuid)",
        "public.sou2ai_change_business_ai_allowance(uuid, integer, integer, text, text)",
        "public.sou2ai_cleanup_security_records(timestamptz, integer)",
    )
    for table in (*protected_tables, *rate_tables):
        op.execute(f"ALTER TABLE public.{table} OWNER TO {MIGRATOR_ROLE}")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM {RUNTIME_ROLE}")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM {OPERATOR_ROLE}")
    for function in functions:
        op.execute(f"ALTER FUNCTION {function} OWNER TO {MIGRATOR_ROLE}")
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM {RUNTIME_ROLE}")
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM {OPERATOR_ROLE}")

    for table in rate_tables:
        op.execute(
            f"GRANT SELECT, INSERT, DELETE ON TABLE public.{table} TO {RUNTIME_ROLE}"
        )
    for function in (
        functions[3],
        functions[4],
        functions[5],
    ):
        op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO {RUNTIME_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {functions[6]} TO {OPERATOR_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {functions[7]} TO {RUNTIME_ROLE}")


def upgrade() -> None:
    _create_rate_limit_tables()
    _create_budget_tables()
    _install_allowance_defaults_and_audit_guards()
    _create_budget_functions()
    _create_allowance_admin_function()
    _create_retention_function()
    _secure_objects()


def downgrade() -> None:
    for signature in (
        "public.sou2ai_cleanup_security_records(timestamptz, integer)",
        "public.sou2ai_change_business_ai_allowance(uuid, integer, integer, text, text)",
        "public.sou2ai_get_current_ai_usage(uuid)",
        "public.sou2ai_reconcile_ai_usage(uuid, integer, integer, boolean, text, text, text)",
        "public.sou2ai_reserve_ai_usage(uuid, uuid, uuid, integer, text, text, integer, integer, integer)",
        "public.sou2ai_charge_expired_ai_reservations(uuid, timestamptz)",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_businesses_default_ai_allowance "
        "ON public.businesses"
    )
    op.execute("DROP FUNCTION IF EXISTS public.sou2ai_create_default_ai_allowance()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_ai_allowance_audit_append_only "
        "ON public.business_ai_allowance_audit"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_ai_allowance_audit_no_truncate "
        "ON public.business_ai_allowance_audit"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.sou2ai_guard_ai_allowance_audit_append_only()"
    )
    op.drop_table("ai_usage_reservations")
    op.drop_table("business_ai_usage_daily")
    op.drop_table("business_ai_allowance_audit")
    op.drop_table("business_ai_allowance_configs")
    op.drop_table("owner_chat_rate_limit_events")
    op.drop_table("registration_rate_limit_events")
