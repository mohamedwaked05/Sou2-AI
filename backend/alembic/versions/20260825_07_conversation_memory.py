"""Add multiple owner conversations and rolling conversation memory.

Revision ID: 20260825_07
Revises: 20260824_06
Create Date: 2026-08-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260825_07"
down_revision: str | None = "20260824_06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATOR_ROLE = "sou2ai_migrator"
RUNTIME_ROLE = "sou2ai_runtime"
OPERATOR_ROLE = "sou2ai_lifecycle_operator"
SUMMARY_RESERVATION_FUNCTION = "public.sou2ai_reserve_conversation_summary_usage(uuid, uuid, integer, integer, integer)"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.owner_conversations "
        "DROP CONSTRAINT owner_conversations_business_id_key"
    )
    op.execute(
        """
        ALTER TABLE public.owner_conversations
          ADD COLUMN creator_user_id uuid,
          ADD COLUMN channel varchar(30) NOT NULL DEFAULT 'owner_web',
          ADD COLUMN title varchar(120) NOT NULL DEFAULT 'New conversation',
          ADD COLUMN last_message_at timestamptz,
          ADD COLUMN archived boolean NOT NULL DEFAULT false,
          ADD COLUMN archived_at timestamptz,
          ADD CONSTRAINT fk_owner_conversations_creator
            FOREIGN KEY (creator_user_id) REFERENCES public.users(id),
          ADD CONSTRAINT uq_owner_conversations_id_business UNIQUE (id, business_id),
          ADD CONSTRAINT ck_owner_conversations_channel
            CHECK (channel = 'owner_web'),
          ADD CONSTRAINT ck_owner_conversations_title
            CHECK (char_length(btrim(title)) BETWEEN 1 AND 120),
          ADD CONSTRAINT ck_owner_conversations_archive
            CHECK ((archived AND archived_at IS NOT NULL)
                OR (NOT archived AND archived_at IS NULL));

        UPDATE public.owner_conversations AS conversation
        SET creator_user_id = business.owner_user_id,
            last_message_at = (
                SELECT max(message.created_at)
                FROM public.owner_chat_messages AS message
                WHERE message.conversation_id = conversation.id
            ),
            title = COALESCE((
                SELECT left(
                    regexp_replace(btrim(message.content), '\\s+', ' ', 'g'), 120
                )
                FROM public.owner_chat_messages AS message
                WHERE message.conversation_id = conversation.id
                  AND message.role = 'owner'
                ORDER BY message.sequence_number, message.id
                LIMIT 1
            ), 'New conversation')
        FROM public.businesses AS business
        WHERE business.id = conversation.business_id;

        ALTER TABLE public.owner_conversations
          ALTER COLUMN creator_user_id SET NOT NULL;

        CREATE INDEX ix_owner_conversations_business_activity
          ON public.owner_conversations (
            business_id, archived, last_message_at DESC NULLS LAST, created_at DESC, id
          );

        CREATE FUNCTION public.sou2ai_guard_owner_conversation()
        RETURNS trigger AS $function$
        BEGIN
            IF TG_OP = 'UPDATE' AND (
                NEW.business_id IS DISTINCT FROM OLD.business_id
                OR NEW.creator_user_id IS DISTINCT FROM OLD.creator_user_id
                OR NEW.channel IS DISTINCT FROM OLD.channel
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
            ) THEN
                RAISE EXCEPTION 'Owner conversation scope is immutable.'
                    USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'INSERT' AND NOT EXISTS (
                SELECT 1 FROM public.business_memberships AS membership
                WHERE membership.business_id = NEW.business_id
                  AND membership.user_id = NEW.creator_user_id
                  AND membership.permission = 'FULL_ACCESS'
            ) THEN
                RAISE EXCEPTION 'Conversation creator lacks business access.'
                    USING ERRCODE = '23514';
            END IF;
            NEW.updated_at = pg_catalog.clock_timestamp();
            RETURN NEW;
        END;
        $function$ LANGUAGE plpgsql SET search_path = pg_catalog;

        CREATE TRIGGER trg_owner_conversation_guard
        BEFORE INSERT OR UPDATE ON public.owner_conversations
        FOR EACH ROW EXECUTE FUNCTION public.sou2ai_guard_owner_conversation();
        """
    )
    op.execute(
        """
        CREATE TABLE public.owner_conversation_summaries (
            id uuid PRIMARY KEY,
            business_id uuid NOT NULL REFERENCES public.businesses(id) ON DELETE CASCADE,
            conversation_id uuid NOT NULL,
            content text,
            summarized_through_sequence_number bigint NOT NULL DEFAULT 0,
            summary_version integer NOT NULL DEFAULT 0,
            last_charged_through_sequence_number bigint NOT NULL DEFAULT 0,
            generation_state varchar(20) NOT NULL DEFAULT 'idle',
            pending_through_sequence_number bigint,
            generation_claim_token uuid,
            generation_claim_expires_at timestamptz,
            generation_attempts integer NOT NULL DEFAULT 0,
            provider_identifier varchar(50),
            model_identifier varchar(100),
            last_failure_code varchar(100),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_owner_summary_conversation UNIQUE (conversation_id),
            CONSTRAINT fk_owner_summary_conversation_scope
              FOREIGN KEY (conversation_id, business_id)
              REFERENCES public.owner_conversations(id, business_id) ON DELETE CASCADE,
            CONSTRAINT ck_owner_summary_content CHECK (
              (summary_version = 0 AND content IS NULL
                AND summarized_through_sequence_number = 0)
              OR (summary_version > 0
                AND char_length(btrim(content)) BETWEEN 1 AND 2000
                AND summarized_through_sequence_number > 0)
            ),
            CONSTRAINT ck_owner_summary_version CHECK (
              summary_version >= 0 AND generation_attempts >= 0
              AND last_charged_through_sequence_number
                  >= summarized_through_sequence_number
            ),
            CONSTRAINT ck_owner_summary_state CHECK (
              generation_state IN ('idle', 'processing', 'failed')
            ),
            CONSTRAINT ck_owner_summary_claim CHECK (
              (generation_state = 'processing'
                AND pending_through_sequence_number > summarized_through_sequence_number
                AND generation_claim_token IS NOT NULL
                AND generation_claim_expires_at IS NOT NULL)
              OR (generation_state <> 'processing'
                AND pending_through_sequence_number IS NULL
                AND generation_claim_token IS NULL
                AND generation_claim_expires_at IS NULL)
            ),
            CONSTRAINT ck_owner_summary_provider CHECK (
              provider_identifier IS NULL OR char_length(provider_identifier) <= 50
            ),
            CONSTRAINT ck_owner_summary_model CHECK (
              model_identifier IS NULL OR char_length(model_identifier) <= 100
            ),
            CONSTRAINT ck_owner_summary_failure CHECK (
              last_failure_code IS NULL OR (
                char_length(last_failure_code) BETWEEN 1 AND 100
                AND last_failure_code ~ '^[a-z][a-z0-9_]*$'
              )
            )
        );
        CREATE INDEX ix_owner_summaries_lease
          ON public.owner_conversation_summaries (
            generation_state, generation_claim_expires_at
          );

        CREATE FUNCTION public.sou2ai_guard_owner_conversation_summary()
        RETURNS trigger AS $function$
        BEGIN
            IF TG_OP = 'UPDATE' AND (
                NEW.business_id IS DISTINCT FROM OLD.business_id
                OR NEW.conversation_id IS DISTINCT FROM OLD.conversation_id
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR NEW.summarized_through_sequence_number
                   < OLD.summarized_through_sequence_number
                OR NEW.summary_version < OLD.summary_version
                OR NEW.last_charged_through_sequence_number
                   < OLD.last_charged_through_sequence_number
            ) THEN
                RAISE EXCEPTION 'Conversation summary scope/checkpoint is immutable.'
                    USING ERRCODE = '23514';
            END IF;
            NEW.updated_at = pg_catalog.clock_timestamp();
            RETURN NEW;
        END;
        $function$ LANGUAGE plpgsql SET search_path = pg_catalog;

        CREATE TRIGGER trg_owner_conversation_summary_guard
        BEFORE UPDATE ON public.owner_conversation_summaries
        FOR EACH ROW EXECUTE FUNCTION public.sou2ai_guard_owner_conversation_summary();
        """
    )
    op.execute(
        """
        ALTER TABLE public.ai_usage_reservations
          ADD COLUMN conversation_summary_id uuid
            REFERENCES public.owner_conversation_summaries(id) ON DELETE SET NULL,
          ADD CONSTRAINT uq_ai_reservation_summary_attempt
            UNIQUE (conversation_summary_id, generation_attempt);
        ALTER TABLE public.ai_usage_reservations
          DROP CONSTRAINT ck_ai_reservation_channel,
          ADD CONSTRAINT ck_ai_reservation_channel
            CHECK (channel IN ('owner', 'customer', 'whatsapp', 'system')),
          ADD CONSTRAINT ck_ai_reservation_subject CHECK (
            (capability = 'conversation_summary'
              AND conversation_summary_id IS NOT NULL
              AND owner_message_id IS NULL
              AND channel = 'system')
            OR (capability <> 'conversation_summary'
              AND conversation_summary_id IS NULL)
          );
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.sou2ai_reserve_conversation_summary_usage(
            target_summary_id uuid,
            target_claim_token uuid,
            target_estimated_input_tokens integer,
            target_max_output_tokens integer,
            target_lease_seconds integer
        ) RETURNS TABLE(
            reservation_id uuid,
            reserved_tokens integer,
            reset_at timestamptz
        ) AS $function$
        DECLARE
            summary_record record;
            reservation_record record;
        BEGIN
            SELECT summary.* INTO summary_record
            FROM public.owner_conversation_summaries AS summary
            JOIN public.businesses AS business ON business.id = summary.business_id
            WHERE summary.id = target_summary_id
              AND summary.generation_state = 'processing'
              AND summary.generation_claim_token = target_claim_token
              AND business.status = 'ACTIVE'
            FOR UPDATE OF summary;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Conversation summary claim was not found.'
                    USING ERRCODE = 'P0002';
            END IF;

            SELECT * INTO reservation_record
            FROM public.sou2ai_reserve_ai_usage(
                summary_record.business_id,
                NULL,
                NULL,
                summary_record.generation_attempts,
                'customer',
                'conversation_summary_pending',
                target_estimated_input_tokens,
                target_max_output_tokens,
                target_lease_seconds
            );
            UPDATE public.ai_usage_reservations
            SET channel = 'system', capability = 'conversation_summary',
                conversation_summary_id = target_summary_id
            WHERE id = reservation_record.reservation_id;
            RETURN QUERY SELECT reservation_record.reservation_id,
                                reservation_record.reserved_tokens,
                                reservation_record.reset_at;
        END;
        $function$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog;
        """
    )
    op.execute(
        f"ALTER TABLE public.owner_conversation_summaries OWNER TO {MIGRATOR_ROLE}"
    )
    op.execute(
        f"REVOKE ALL ON TABLE public.owner_conversation_summaries "
        f"FROM PUBLIC, {OPERATOR_ROLE}"
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON TABLE public.owner_conversation_summaries "
        f"TO {RUNTIME_ROLE}"
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON TABLE public.owner_conversations "
        f"TO {RUNTIME_ROLE}"
    )
    op.execute(
        f"REVOKE DELETE, TRUNCATE ON TABLE public.owner_conversations "
        f"FROM {RUNTIME_ROLE}"
    )
    for function in (
        "public.sou2ai_guard_owner_conversation()",
        "public.sou2ai_guard_owner_conversation_summary()",
        SUMMARY_RESERVATION_FUNCTION,
    ):
        op.execute(f"ALTER FUNCTION {function} OWNER TO {MIGRATOR_ROLE}")
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM {OPERATOR_ROLE}")
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM {RUNTIME_ROLE}")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {SUMMARY_RESERVATION_FUNCTION} TO {RUNTIME_ROLE}"
    )


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {SUMMARY_RESERVATION_FUNCTION}")
    op.execute(
        "UPDATE public.ai_usage_reservations "
        "SET channel = 'customer', capability = 'conversation_summary_legacy', "
        "conversation_summary_id = NULL "
        "WHERE capability = 'conversation_summary'"
    )
    op.execute(
        "ALTER TABLE public.ai_usage_reservations "
        "DROP CONSTRAINT ck_ai_reservation_subject, "
        "DROP CONSTRAINT ck_ai_reservation_channel, "
        "ADD CONSTRAINT ck_ai_reservation_channel "
        "CHECK (channel IN ('owner', 'customer', 'whatsapp')), "
        "DROP CONSTRAINT uq_ai_reservation_summary_attempt, "
        "DROP COLUMN conversation_summary_id"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_owner_conversation_summary_guard "
        "ON public.owner_conversation_summaries"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.sou2ai_guard_owner_conversation_summary()"
    )
    op.execute("DROP TABLE public.owner_conversation_summaries")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_owner_conversation_guard "
        "ON public.owner_conversations"
    )
    op.execute("DROP FUNCTION IF EXISTS public.sou2ai_guard_owner_conversation()")
    op.execute(
        """
        DO $function$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM public.owner_conversations
            GROUP BY business_id HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION 'Cannot downgrade while businesses have multiple conversations.';
          END IF;
        END;
        $function$;
        DROP INDEX public.ix_owner_conversations_business_activity;
        ALTER TABLE public.owner_conversations
          DROP CONSTRAINT ck_owner_conversations_archive,
          DROP CONSTRAINT ck_owner_conversations_title,
          DROP CONSTRAINT ck_owner_conversations_channel,
          DROP CONSTRAINT uq_owner_conversations_id_business,
          DROP CONSTRAINT fk_owner_conversations_creator,
          DROP COLUMN archived_at,
          DROP COLUMN archived,
          DROP COLUMN last_message_at,
          DROP COLUMN title,
          DROP COLUMN channel,
          DROP COLUMN creator_user_id,
          ADD CONSTRAINT owner_conversations_business_id_key UNIQUE (business_id);
        """
    )
    op.execute(f"GRANT DELETE ON TABLE public.owner_conversations TO {RUNTIME_ROLE}")
