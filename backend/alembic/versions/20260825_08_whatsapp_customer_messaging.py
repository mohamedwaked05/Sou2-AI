"""Add tenant-scoped WhatsApp customer messaging.

Revision ID: 20260825_08
Revises: 20260825_07
Create Date: 2026-08-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260825_08"
down_revision: str | None = "20260825_07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATOR_ROLE = "sou2ai_migrator"
RUNTIME_ROLE = "sou2ai_runtime"
OPERATOR_ROLE = "sou2ai_lifecycle_operator"
CUSTOMER_RESERVATION_FUNCTION = (
    "public.sou2ai_reserve_customer_message_usage(uuid, integer, integer, integer)"
)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.business_knowledge "
        "ADD COLUMN customer_visible boolean NOT NULL DEFAULT false; "
        "ALTER TABLE public.knowledge_documents "
        "ADD COLUMN customer_visible boolean NOT NULL DEFAULT false"
    )
    op.execute(
        """
        CREATE TABLE public.messaging_channel_connections (
          id uuid PRIMARY KEY,
          business_id uuid NOT NULL REFERENCES public.businesses(id) ON DELETE CASCADE,
          provider_type varchar(40) NOT NULL,
          display_name varchar(120) NOT NULL,
          connection_profile_key varchar(100) NOT NULL,
          external_phone_number_id varchar(100),
          status varchar(20) NOT NULL DEFAULT 'CONFIGURED',
          auto_reply_enabled boolean NOT NULL DEFAULT false,
          last_validated_at timestamptz,
          last_successful_health_check_at timestamptz,
          failure_code varchar(100),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT uq_channel_id_business UNIQUE (id, business_id),
          CONSTRAINT uq_channel_external_phone_number UNIQUE (external_phone_number_id),
          CONSTRAINT ck_channel_provider CHECK (provider_type = 'meta_whatsapp'),
          CONSTRAINT ck_channel_profile CHECK (connection_profile_key = 'meta_whatsapp_cloud'),
          CONSTRAINT ck_channel_display_name CHECK (char_length(btrim(display_name)) BETWEEN 2 AND 120),
          CONSTRAINT ck_channel_status CHECK (status IN ('CONFIGURED','VALIDATED','ACTIVE','UNHEALTHY','DISABLED')),
          CONSTRAINT ck_channel_failure_code CHECK (failure_code IS NULL OR (
            char_length(failure_code) BETWEEN 1 AND 100
            AND failure_code ~ '^[a-z][a-z0-9_]*(\\.[a-z0-9_]+)*$'
          )),
          CONSTRAINT ck_channel_lifecycle CHECK (
            (status = 'CONFIGURED' AND external_phone_number_id IS NULL
              AND last_validated_at IS NULL AND last_successful_health_check_at IS NULL
              AND failure_code IS NULL AND NOT auto_reply_enabled)
            OR (status IN ('VALIDATED','ACTIVE') AND external_phone_number_id IS NOT NULL
              AND last_validated_at IS NOT NULL
              AND last_successful_health_check_at IS NOT NULL AND failure_code IS NULL)
            OR (status = 'UNHEALTHY' AND last_validated_at IS NOT NULL
              AND failure_code IS NOT NULL AND NOT auto_reply_enabled)
            OR (status = 'DISABLED' AND failure_code IS NULL AND NOT auto_reply_enabled)
          )
        );
        CREATE INDEX ix_channel_business_created
          ON public.messaging_channel_connections (business_id, created_at, id);
        CREATE UNIQUE INDEX uq_channel_active_provider
          ON public.messaging_channel_connections (business_id, provider_type)
          WHERE status = 'ACTIVE';

        CREATE TABLE public.customer_conversations (
          id uuid PRIMARY KEY,
          business_id uuid NOT NULL REFERENCES public.businesses(id) ON DELETE CASCADE,
          connection_id uuid NOT NULL,
          customer_identity_hash varchar(64) NOT NULL,
          encrypted_customer_identity text NOT NULL,
          masked_customer_label varchar(40) NOT NULL,
          state varchar(20) NOT NULL DEFAULT 'AI_ACTIVE',
          last_message_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT uq_customer_conversation_scope UNIQUE (id, business_id),
          CONSTRAINT uq_customer_identity UNIQUE (connection_id, customer_identity_hash),
          CONSTRAINT fk_customer_conversation_channel_scope
            FOREIGN KEY (connection_id, business_id)
            REFERENCES public.messaging_channel_connections(id, business_id)
            ON DELETE CASCADE,
          CONSTRAINT ck_customer_conversation_state CHECK (state IN ('AI_ACTIVE','HUMAN_HANDOFF')),
          CONSTRAINT ck_customer_identity_hash CHECK (customer_identity_hash ~ '^[0-9a-f]{64}$'),
          CONSTRAINT ck_customer_masked_label CHECK (
            char_length(btrim(masked_customer_label)) BETWEEN 3 AND 40
          )
        );
        CREATE INDEX ix_customer_conversation_activity
          ON public.customer_conversations (
            business_id, state, last_message_at DESC NULLS LAST, id
          );

        CREATE TABLE public.customer_messages (
          id uuid PRIMARY KEY,
          business_id uuid NOT NULL REFERENCES public.businesses(id) ON DELETE CASCADE,
          conversation_id uuid NOT NULL,
          direction varchar(10) NOT NULL,
          sender varchar(10) NOT NULL,
          content text NOT NULL,
          status varchar(20) NOT NULL,
          provider_message_id varchar(200),
          reply_to_message_id uuid REFERENCES public.customer_messages(id) ON DELETE SET NULL,
          send_attempts integer NOT NULL DEFAULT 0,
          next_attempt_at timestamptz,
          failure_code varchar(100),
          provider_timestamp timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT fk_customer_message_conversation_scope
            FOREIGN KEY (conversation_id, business_id)
            REFERENCES public.customer_conversations(id, business_id) ON DELETE CASCADE,
          CONSTRAINT ck_customer_message_direction CHECK (direction IN ('inbound','outbound')),
          CONSTRAINT ck_customer_message_sender CHECK (sender IN ('customer','ai','owner')),
          CONSTRAINT ck_customer_message_status CHECK (status IN (
            'RECEIVED','PROCESSING','COMPLETED','PENDING_SEND','SENDING',
            'SENT','DELIVERED','READ','FAILED'
          )),
          CONSTRAINT ck_customer_message_content CHECK (
            char_length(btrim(content)) BETWEEN 1 AND 4000
          ),
          CONSTRAINT ck_customer_send_attempts CHECK (send_attempts BETWEEN 0 AND 3),
          CONSTRAINT ck_customer_message_failure_code CHECK (
            failure_code IS NULL OR failure_code ~ '^[a-z][a-z0-9_]*(\\.[a-z0-9_]+)*$'
          ),
          CONSTRAINT ck_customer_message_semantics CHECK (
            (direction = 'inbound' AND sender = 'customer'
              AND provider_message_id IS NOT NULL
              AND status IN ('RECEIVED','PROCESSING','COMPLETED','FAILED'))
            OR (direction = 'outbound' AND sender IN ('ai','owner')
              AND status IN ('PENDING_SEND','SENDING','SENT','DELIVERED','READ','FAILED'))
          ),
          CONSTRAINT uq_customer_provider_message UNIQUE (provider_message_id),
          CONSTRAINT uq_customer_reply_once UNIQUE (reply_to_message_id)
        );
        CREATE INDEX ix_customer_messages_history
          ON public.customer_messages (conversation_id, created_at, id);
        CREATE INDEX ix_customer_messages_outbox
          ON public.customer_messages (status, next_attempt_at, id);

        CREATE TABLE public.inbound_webhook_deliveries (
          id uuid PRIMARY KEY,
          business_id uuid NOT NULL REFERENCES public.businesses(id) ON DELETE CASCADE,
          connection_id uuid NOT NULL REFERENCES public.messaging_channel_connections(id) ON DELETE CASCADE,
          provider_event_id varchar(200) NOT NULL,
          event_kind varchar(30) NOT NULL,
          status varchar(20) NOT NULL,
          customer_message_id uuid REFERENCES public.customer_messages(id) ON DELETE SET NULL,
          failure_code varchar(100),
          received_at timestamptz NOT NULL DEFAULT now(),
          processed_at timestamptz,
          CONSTRAINT uq_webhook_provider_event UNIQUE (provider_event_id),
          CONSTRAINT ck_webhook_delivery_status CHECK (status IN ('QUEUED','PROCESSED','IGNORED','FAILED')),
          CONSTRAINT ck_webhook_delivery_kind CHECK (event_kind IN ('message','status'))
        );
        CREATE INDEX ix_webhook_connection_received
          ON public.inbound_webhook_deliveries (connection_id, received_at, id);

        CREATE TABLE public.customer_generation_rate_events (
          id uuid PRIMARY KEY,
          business_id uuid NOT NULL REFERENCES public.businesses(id) ON DELETE CASCADE,
          conversation_id uuid NOT NULL REFERENCES public.customer_conversations(id) ON DELETE CASCADE,
          customer_message_id uuid NOT NULL REFERENCES public.customer_messages(id) ON DELETE CASCADE,
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT uq_customer_rate_message UNIQUE (customer_message_id)
        );
        CREATE INDEX ix_customer_rate_business_created
          ON public.customer_generation_rate_events (business_id, created_at);
        CREATE INDEX ix_customer_rate_conversation_created
          ON public.customer_generation_rate_events (conversation_id, created_at);
        """
    )
    op.execute(
        """
        ALTER TABLE public.ai_usage_reservations
          ADD COLUMN customer_message_id uuid REFERENCES public.customer_messages(id) ON DELETE SET NULL,
          ADD CONSTRAINT uq_ai_reservation_customer_message_attempt
            UNIQUE (customer_message_id, generation_attempt);
        ALTER TABLE public.ai_usage_reservations
          DROP CONSTRAINT ck_ai_reservation_subject,
          ADD CONSTRAINT ck_ai_reservation_subject CHECK (
            (capability = 'conversation_summary'
              AND conversation_summary_id IS NOT NULL AND owner_message_id IS NULL
              AND customer_message_id IS NULL AND channel = 'system')
            OR (capability = 'customer_chat'
              AND customer_message_id IS NOT NULL AND owner_message_id IS NULL
              AND conversation_summary_id IS NULL AND channel = 'whatsapp')
            OR (capability NOT IN ('conversation_summary','customer_chat')
              AND conversation_summary_id IS NULL AND customer_message_id IS NULL)
          );
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.sou2ai_reserve_customer_message_usage(
          target_message_id uuid,
          target_estimated_input_tokens integer,
          target_max_output_tokens integer,
          target_lease_seconds integer
        ) RETURNS TABLE(
          reservation_id uuid,
          reserved_tokens integer,
          reset_at timestamptz
        ) AS $function$
        DECLARE
          message_record record;
          reservation_record record;
          existing_record record;
        BEGIN
          SELECT reservation.* INTO existing_record
          FROM public.ai_usage_reservations AS reservation
          WHERE reservation.customer_message_id = target_message_id
            AND reservation.generation_attempt = 1;
          IF FOUND THEN
            RETURN QUERY SELECT existing_record.id, existing_record.reserved_tokens,
                                existing_record.window_end;
            RETURN;
          END IF;

          SELECT message.* INTO message_record
          FROM public.customer_messages AS message
          JOIN public.businesses AS business ON business.id = message.business_id
          WHERE message.id = target_message_id
            AND message.direction = 'inbound'
            AND message.status = 'PROCESSING'
            AND business.status = 'ACTIVE'
          FOR UPDATE OF message;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'Customer message is unavailable.' USING ERRCODE = 'P0002';
          END IF;

          SELECT * INTO reservation_record
          FROM public.sou2ai_reserve_ai_usage(
            message_record.business_id, NULL, NULL, 1, 'whatsapp',
            'customer_chat_pending', target_estimated_input_tokens,
            target_max_output_tokens, target_lease_seconds
          );
          UPDATE public.ai_usage_reservations
          SET capability = 'customer_chat', customer_message_id = target_message_id
          WHERE id = reservation_record.reservation_id;
          RETURN QUERY SELECT reservation_record.reservation_id,
                              reservation_record.reserved_tokens,
                              reservation_record.reset_at;
        END;
        $function$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog;
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.sou2ai_guard_customer_channel()
        RETURNS trigger AS $function$
        BEGIN
          IF TG_OP = 'UPDATE' AND (
            NEW.business_id IS DISTINCT FROM OLD.business_id
            OR NEW.provider_type IS DISTINCT FROM OLD.provider_type
            OR NEW.connection_profile_key IS DISTINCT FROM OLD.connection_profile_key
            OR NEW.created_at IS DISTINCT FROM OLD.created_at
          ) THEN
            RAISE EXCEPTION 'Messaging channel scope is immutable.' USING ERRCODE = '23514';
          END IF;
          NEW.updated_at = pg_catalog.clock_timestamp();
          RETURN NEW;
        END;
        $function$ LANGUAGE plpgsql SET search_path = pg_catalog;
        CREATE TRIGGER trg_customer_channel_guard
          BEFORE UPDATE ON public.messaging_channel_connections
          FOR EACH ROW EXECUTE FUNCTION public.sou2ai_guard_customer_channel();

        CREATE FUNCTION public.sou2ai_guard_customer_conversation()
        RETURNS trigger AS $function$
        BEGIN
          IF TG_OP = 'UPDATE' AND (
            NEW.business_id IS DISTINCT FROM OLD.business_id
            OR NEW.connection_id IS DISTINCT FROM OLD.connection_id
            OR NEW.customer_identity_hash IS DISTINCT FROM OLD.customer_identity_hash
            OR NEW.encrypted_customer_identity IS DISTINCT FROM OLD.encrypted_customer_identity
            OR NEW.masked_customer_label IS DISTINCT FROM OLD.masked_customer_label
            OR NEW.created_at IS DISTINCT FROM OLD.created_at
          ) THEN
            RAISE EXCEPTION 'Customer conversation scope is immutable.' USING ERRCODE = '23514';
          END IF;
          NEW.updated_at = pg_catalog.clock_timestamp();
          RETURN NEW;
        END;
        $function$ LANGUAGE plpgsql SET search_path = pg_catalog;
        CREATE TRIGGER trg_customer_conversation_guard
          BEFORE UPDATE ON public.customer_conversations
          FOR EACH ROW EXECUTE FUNCTION public.sou2ai_guard_customer_conversation();

        CREATE FUNCTION public.sou2ai_guard_customer_message()
        RETURNS trigger AS $function$
        BEGIN
          IF TG_OP = 'UPDATE' AND (
            NEW.business_id IS DISTINCT FROM OLD.business_id
            OR NEW.conversation_id IS DISTINCT FROM OLD.conversation_id
            OR NEW.direction IS DISTINCT FROM OLD.direction
            OR NEW.sender IS DISTINCT FROM OLD.sender
            OR NEW.content IS DISTINCT FROM OLD.content
            OR NEW.reply_to_message_id IS DISTINCT FROM OLD.reply_to_message_id
            OR NEW.provider_timestamp IS DISTINCT FROM OLD.provider_timestamp
            OR NEW.created_at IS DISTINCT FROM OLD.created_at
          ) THEN
            RAISE EXCEPTION 'Customer message content and scope are immutable.' USING ERRCODE = '23514';
          END IF;
          NEW.updated_at = pg_catalog.clock_timestamp();
          RETURN NEW;
        END;
        $function$ LANGUAGE plpgsql SET search_path = pg_catalog;
        CREATE TRIGGER trg_customer_message_guard
          BEFORE UPDATE ON public.customer_messages
          FOR EACH ROW EXECUTE FUNCTION public.sou2ai_guard_customer_message();
        """
    )
    tables = (
        "messaging_channel_connections",
        "customer_conversations",
        "customer_messages",
        "inbound_webhook_deliveries",
        "customer_generation_rate_events",
    )
    for table in tables:
        op.execute(f"ALTER TABLE public.{table} OWNER TO {MIGRATOR_ROLE}")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, {OPERATOR_ROLE}")
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE ON TABLE public.{table} TO {RUNTIME_ROLE}"
        )
        op.execute(
            f"REVOKE DELETE, TRUNCATE ON TABLE public.{table} FROM {RUNTIME_ROLE}"
        )
    functions = (
        CUSTOMER_RESERVATION_FUNCTION,
        "public.sou2ai_guard_customer_channel()",
        "public.sou2ai_guard_customer_conversation()",
        "public.sou2ai_guard_customer_message()",
    )
    for function in functions:
        op.execute(f"ALTER FUNCTION {function} OWNER TO {MIGRATOR_ROLE}")
        op.execute(
            f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC, {OPERATOR_ROLE}, {RUNTIME_ROLE}"
        )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {CUSTOMER_RESERVATION_FUNCTION} TO {RUNTIME_ROLE}"
    )


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {CUSTOMER_RESERVATION_FUNCTION}")
    op.execute(
        "DELETE FROM public.ai_usage_reservations WHERE customer_message_id IS NOT NULL; "
        "ALTER TABLE public.ai_usage_reservations DROP CONSTRAINT ck_ai_reservation_subject, "
        "ADD CONSTRAINT ck_ai_reservation_subject CHECK ("
        "(capability = 'conversation_summary' AND conversation_summary_id IS NOT NULL "
        "AND owner_message_id IS NULL AND channel = 'system') OR "
        "(capability <> 'conversation_summary' AND conversation_summary_id IS NULL)), "
        "DROP CONSTRAINT uq_ai_reservation_customer_message_attempt, "
        "DROP COLUMN customer_message_id"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_customer_message_guard ON public.customer_messages"
    )
    op.execute("DROP FUNCTION IF EXISTS public.sou2ai_guard_customer_message()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_customer_conversation_guard ON public.customer_conversations"
    )
    op.execute("DROP FUNCTION IF EXISTS public.sou2ai_guard_customer_conversation()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_customer_channel_guard ON public.messaging_channel_connections"
    )
    op.execute("DROP FUNCTION IF EXISTS public.sou2ai_guard_customer_channel()")
    op.execute("DROP TABLE public.customer_generation_rate_events")
    op.execute("DROP TABLE public.inbound_webhook_deliveries")
    op.execute("DROP TABLE public.customer_messages")
    op.execute("DROP TABLE public.customer_conversations")
    op.execute("DROP TABLE public.messaging_channel_connections")
    op.execute("ALTER TABLE public.knowledge_documents DROP COLUMN customer_visible")
    op.execute("ALTER TABLE public.business_knowledge DROP COLUMN customer_visible")
