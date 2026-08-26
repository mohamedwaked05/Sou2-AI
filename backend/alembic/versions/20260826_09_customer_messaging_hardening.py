"""Harden customer messaging leases and tenant-scoped relationships.

Revision ID: 20260826_09
Revises: 20260825_08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260826_09"
down_revision: str | None = "20260825_08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.messaging_channel_connections
          DROP CONSTRAINT ck_channel_profile,
          ADD CONSTRAINT ck_channel_profile
            CHECK (connection_profile_key ~ '^[a-z][a-z0-9_]*$');
        ALTER TABLE public.customer_messages
          ADD COLUMN claim_expires_at timestamptz,
          ADD CONSTRAINT uq_customer_message_business_scope
            UNIQUE (id, business_id),
          ADD CONSTRAINT uq_customer_message_scope
            UNIQUE (id, conversation_id, business_id),
          ADD CONSTRAINT fk_customer_message_reply_scope
            FOREIGN KEY (reply_to_message_id, conversation_id, business_id)
            REFERENCES public.customer_messages(id, conversation_id, business_id)
            ON DELETE SET NULL;
        CREATE INDEX ix_customer_messages_claims
          ON public.customer_messages (status, claim_expires_at, id);
        ALTER TABLE public.inbound_webhook_deliveries
          ADD CONSTRAINT fk_webhook_connection_scope
            FOREIGN KEY (connection_id, business_id)
            REFERENCES public.messaging_channel_connections(id, business_id)
            ON DELETE CASCADE,
          ADD CONSTRAINT fk_webhook_message_scope
            FOREIGN KEY (customer_message_id, business_id)
            REFERENCES public.customer_messages(id, business_id)
            ON DELETE SET NULL;
        ALTER TABLE public.customer_generation_rate_events
          ADD CONSTRAINT fk_customer_rate_conversation_scope
            FOREIGN KEY (conversation_id, business_id)
            REFERENCES public.customer_conversations(id, business_id)
            ON DELETE CASCADE,
          ADD CONSTRAINT fk_customer_rate_message_scope
            FOREIGN KEY (customer_message_id, conversation_id, business_id)
            REFERENCES public.customer_messages(id, conversation_id, business_id)
            ON DELETE CASCADE;
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.sou2ai_guard_webhook_scope()
        RETURNS trigger AS $function$
        BEGIN
          IF NEW.customer_message_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.customer_messages message
            WHERE message.id = NEW.customer_message_id
              AND message.business_id = NEW.business_id
              AND message.conversation_id IS NOT NULL
          ) THEN
            RAISE EXCEPTION 'Webhook message scope is invalid.' USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END;
        $function$ LANGUAGE plpgsql SET search_path = pg_catalog;
        CREATE TRIGGER trg_webhook_scope_guard
          BEFORE INSERT OR UPDATE ON public.inbound_webhook_deliveries
          FOR EACH ROW EXECUTE FUNCTION public.sou2ai_guard_webhook_scope();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_webhook_scope_guard
          ON public.inbound_webhook_deliveries;
        DROP FUNCTION IF EXISTS public.sou2ai_guard_webhook_scope();
        ALTER TABLE public.customer_generation_rate_events
          DROP CONSTRAINT fk_customer_rate_message_scope,
          DROP CONSTRAINT fk_customer_rate_conversation_scope;
        ALTER TABLE public.inbound_webhook_deliveries
          DROP CONSTRAINT fk_webhook_message_scope,
          DROP CONSTRAINT fk_webhook_connection_scope;
        DROP INDEX IF EXISTS public.ix_customer_messages_claims;
        ALTER TABLE public.customer_messages
          DROP CONSTRAINT fk_customer_message_reply_scope,
          DROP CONSTRAINT uq_customer_message_business_scope,
          DROP CONSTRAINT uq_customer_message_scope,
          DROP COLUMN claim_expires_at;
        ALTER TABLE public.messaging_channel_connections
          DROP CONSTRAINT ck_channel_profile,
          ADD CONSTRAINT ck_channel_profile
            CHECK (connection_profile_key = 'meta_whatsapp_cloud');
        """
    )
