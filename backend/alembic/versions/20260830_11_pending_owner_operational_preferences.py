"""Persist resumable owner preference clarifications."""

from collections.abc import Sequence

from alembic import op

MIGRATOR_ROLE = "sou2ai_migrator"
RUNTIME_ROLE = "sou2ai_runtime"
OPERATOR_ROLE = "sou2ai_lifecycle_operator"

revision: str = "20260830_11"
down_revision: str | None = "20260827_10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE public.pending_owner_operational_preferences (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
            business_id uuid NOT NULL REFERENCES public.businesses(id) ON DELETE CASCADE,
            source_id uuid NOT NULL,
            conversation_id uuid NOT NULL,
            originating_message_id uuid NOT NULL,
            operation varchar(32) NOT NULL,
            preference_key varchar(64) NOT NULL,
            expected_field varchar(32) NOT NULL,
            candidate_references jsonb NOT NULL,
            state varchar(16) NOT NULL DEFAULT 'pending',
            version integer NOT NULL DEFAULT 1,
            expires_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_pending_owner_preference_operation
                CHECK (operation = 'set_preference'),
            CONSTRAINT ck_pending_owner_preference_key
                CHECK (preference_key = 'default_inventory_location'),
            CONSTRAINT ck_pending_owner_preference_expected_field
                CHECK (expected_field = 'location'),
            CONSTRAINT ck_pending_owner_preference_state
                CHECK (state IN ('pending', 'completed', 'superseded', 'expired', 'invalidated')),
            CONSTRAINT ck_pending_owner_preference_version CHECK (version > 0),
            CONSTRAINT ck_pending_owner_preference_expiry CHECK (expires_at > created_at),
            CONSTRAINT fk_pending_owner_preference_source_scope
                FOREIGN KEY (source_id, business_id)
                REFERENCES public.operational_data_sources(id, business_id)
                ON DELETE CASCADE,
            CONSTRAINT fk_pending_owner_preference_conversation_scope
                FOREIGN KEY (conversation_id, business_id)
                REFERENCES public.owner_conversations(id, business_id)
                ON DELETE CASCADE,
            CONSTRAINT fk_pending_owner_preference_originating_message
                FOREIGN KEY (conversation_id, originating_message_id)
                REFERENCES public.owner_chat_messages(conversation_id, id)
                ON DELETE CASCADE
        );
        CREATE UNIQUE INDEX uq_pending_owner_preference_active_scope
            ON public.pending_owner_operational_preferences
                (user_id, business_id, source_id, conversation_id, preference_key)
            WHERE state = 'pending';
        CREATE INDEX ix_pending_owner_preference_lookup
            ON public.pending_owner_operational_preferences
                (user_id, business_id, conversation_id, state, expires_at);
        """
    )
    op.execute(
        f"""
        ALTER TABLE public.pending_owner_operational_preferences OWNER TO {MIGRATOR_ROLE};
        REVOKE ALL ON TABLE public.pending_owner_operational_preferences
            FROM PUBLIC, {OPERATOR_ROLE};
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON TABLE public.pending_owner_operational_preferences TO {RUNTIME_ROLE};
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.pending_owner_operational_preferences")
