"""Narrow reservation guards for the restricted API/worker runtime.

Additive compatibility migration; no table grants or historical data changes.
"""

from alembic import op

revision = "20261013_17"
down_revision = "20261007_16"
branch_labels = None
depends_on = None


LOCK_FUNCTION = "public.sou2ai_lock_active_ai_usage_reservation(uuid, timestamptz)"
SUMMARY_FUNCTION = "public.sou2ai_summary_attempt_has_usage(uuid, uuid, uuid, integer)"


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION public.sou2ai_lock_active_ai_usage_reservation(
            target_reservation_id uuid,
            target_finalization_deadline timestamptz
        ) RETURNS TABLE(lease_expires_at timestamptz, window_end timestamptz)
        AS $function$
        DECLARE
            reservation_record record;
            checked_at timestamptz;
        BEGIN
            SELECT r.status, r.lease_expires_at, r.window_end
            INTO reservation_record
            FROM public.ai_usage_reservations AS r
            WHERE r.id = target_reservation_id
            FOR UPDATE OF r;

            -- Recheck after a contested lock; transaction start time is unsafe.
            checked_at := pg_catalog.clock_timestamp();
            IF NOT FOUND OR reservation_record.status <> 'reserved'
                OR reservation_record.lease_expires_at <= checked_at
                OR reservation_record.window_end <= checked_at
                OR (target_finalization_deadline IS NOT NULL
                    AND target_finalization_deadline <= checked_at) THEN
                RETURN;
            END IF;
            RETURN QUERY SELECT reservation_record.lease_expires_at,
                                reservation_record.window_end;
        END;
        $function$ LANGUAGE plpgsql VOLATILE SECURITY DEFINER
            SET search_path = pg_catalog;
    """)
    op.execute("""
        CREATE FUNCTION public.sou2ai_summary_attempt_has_usage(
            target_summary_id uuid,
            target_business_id uuid,
            target_conversation_id uuid,
            target_generation_attempt integer
        ) RETURNS boolean AS $function$
            SELECT EXISTS (
                SELECT 1
                FROM public.owner_conversation_summaries AS s
                JOIN public.owner_conversations AS c
                    ON c.id = s.conversation_id AND c.business_id = s.business_id
                JOIN public.ai_usage_reservations AS r
                    ON r.conversation_summary_id = s.id
                    AND r.business_id = s.business_id
                WHERE s.id = target_summary_id
                    AND s.business_id = target_business_id
                    AND s.conversation_id = target_conversation_id
                    AND s.generation_attempts = target_generation_attempt
                    AND r.generation_attempt = target_generation_attempt
                    AND r.status <> 'released'
            );
        $function$ LANGUAGE sql STABLE SECURITY DEFINER
            SET search_path = pg_catalog;
    """)
    for function in (LOCK_FUNCTION, SUMMARY_FUNCTION):
        op.execute(f"ALTER FUNCTION {function} OWNER TO sou2ai_migrator")
        op.execute(
            f"REVOKE ALL ON FUNCTION {function} "
            "FROM PUBLIC, sou2ai_runtime, sou2ai_lifecycle_operator"
        )
        op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO sou2ai_runtime")


def downgrade() -> None:
    # Old application versions can retain the additive functions on rollback.
    # Remove them only after all dependent API/worker processes are retired.
    op.execute(f"DROP FUNCTION {SUMMARY_FUNCTION}")
    op.execute(f"DROP FUNCTION {LOCK_FUNCTION}")
