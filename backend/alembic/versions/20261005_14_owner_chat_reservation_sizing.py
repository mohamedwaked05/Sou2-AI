"""Resize an owner turn's reservation before each subsequent provider call.

Revision ID: 20261005_14
Revises: 20260930_13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261005_14"
down_revision: str | None = "20260930_13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNCTION = "public.sou2ai_resize_owner_chat_usage(uuid, uuid, integer, integer)"


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION public.sou2ai_resize_owner_chat_usage(
            target_reservation_id uuid,
            target_claim_token uuid,
            target_estimated_input_tokens integer,
            target_max_output_tokens integer
        ) RETURNS void AS $function$
        DECLARE
            target_business_id uuid;
            allowance integer;
            reservation_record record;
            usage_record record;
            requested_tokens integer;
        BEGIN
            IF target_claim_token IS NULL
               OR target_estimated_input_tokens IS NULL
               OR target_estimated_input_tokens < 0
               OR target_max_output_tokens IS NULL
               OR target_max_output_tokens < 1 THEN
                RAISE EXCEPTION 'Invalid AI usage reservation.'
                    USING ERRCODE = '22023';
            END IF;
            SELECT business_id INTO target_business_id
            FROM public.ai_usage_reservations WHERE id = target_reservation_id;

            -- Match initial admission's lock order; all channels share this lock.
            SELECT daily_token_allowance INTO allowance
            FROM public.business_ai_allowance_configs
            WHERE business_id = target_business_id FOR UPDATE;
            SELECT * INTO reservation_record
            FROM public.ai_usage_reservations
            WHERE id = target_reservation_id FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'AI usage reservation was not found.'
                    USING ERRCODE = 'P0002';
            END IF;
            IF reservation_record.channel <> 'owner'
               OR reservation_record.capability <> 'owner_chat'
               OR reservation_record.status <> 'reserved'
               OR reservation_record.lease_expires_at <= pg_catalog.clock_timestamp()
               OR reservation_record.window_end <= pg_catalog.clock_timestamp()
               OR NOT EXISTS (
                   SELECT 1 FROM public.owner_chat_messages AS message
                   JOIN public.owner_conversations AS conversation
                     ON conversation.id = message.conversation_id
                   WHERE message.id = reservation_record.owner_message_id
                     AND conversation.business_id = target_business_id
                     AND message.generation_state = 'processing'
                     AND message.generation_attempts = reservation_record.generation_attempt
                     AND message.generation_claim_token = target_claim_token
                     AND message.generation_claim_expires_at > pg_catalog.clock_timestamp()
               ) THEN
                RAISE EXCEPTION 'AI usage reservation is not an active owner claim.'
                    USING ERRCODE = '22023';
            END IF;
            SELECT total_tokens_used, tokens_reserved INTO usage_record
            FROM public.business_ai_usage_daily
            WHERE business_id = target_business_id
              AND window_start = reservation_record.window_start FOR UPDATE;
            requested_tokens := target_estimated_input_tokens + target_max_output_tokens;
            IF usage_record.total_tokens_used + usage_record.tokens_reserved
               - reservation_record.reserved_tokens + requested_tokens > allowance THEN
                RAISE EXCEPTION 'daily_ai_token_limit_reached'
                    USING ERRCODE = 'P0001';
            END IF;
            UPDATE public.business_ai_usage_daily
            SET tokens_reserved = tokens_reserved - reservation_record.reserved_tokens
                    + requested_tokens,
                updated_at = pg_catalog.clock_timestamp()
            WHERE business_id = target_business_id
              AND window_start = reservation_record.window_start;
            UPDATE public.ai_usage_reservations
            SET estimated_input_tokens = target_estimated_input_tokens,
                max_output_tokens = target_max_output_tokens,
                reserved_tokens = requested_tokens
            WHERE id = target_reservation_id;
        END;
        $function$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog;
        """
    )
    op.execute(f"ALTER FUNCTION {FUNCTION} OWNER TO sou2ai_migrator")
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO sou2ai_runtime")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {FUNCTION}")
