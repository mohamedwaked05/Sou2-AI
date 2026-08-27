"""Persist validated user operational preferences."""

from collections.abc import Sequence

from alembic import op

MIGRATOR_ROLE = "sou2ai_migrator"
RUNTIME_ROLE = "sou2ai_runtime"
OPERATOR_ROLE = "sou2ai_lifecycle_operator"

revision: str = "20260827_10"
down_revision: str | None = "20260826_09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE public.user_operational_preferences (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
            business_id uuid NOT NULL REFERENCES public.businesses(id) ON DELETE CASCADE,
            source_id uuid NOT NULL,
            preference_key varchar(64) NOT NULL,
            location_type varchar(16) NOT NULL,
            location_external_id varchar(128) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_user_operational_preference_key
                CHECK (preference_key = 'default_inventory_location'),
            CONSTRAINT ck_user_operational_preference_location_type
                CHECK (location_type IN ('branch', 'warehouse')),
            CONSTRAINT uq_user_operational_preference_scope
                UNIQUE (user_id, business_id, source_id, preference_key),
            CONSTRAINT fk_user_operational_preference_source_scope
                FOREIGN KEY (source_id, business_id)
                REFERENCES public.operational_data_sources(id, business_id)
                ON DELETE CASCADE
        );
        CREATE INDEX ix_user_operational_preferences_lookup
            ON public.user_operational_preferences (user_id, business_id, preference_key);
        """
    )
    op.execute(
        f"""
        ALTER TABLE public.user_operational_preferences OWNER TO {MIGRATOR_ROLE};
        REVOKE ALL ON TABLE public.user_operational_preferences
            FROM PUBLIC, {OPERATOR_ROLE};
        GRANT SELECT, INSERT, DELETE ON TABLE public.user_operational_preferences
            TO {RUNTIME_ROLE};
        GRANT UPDATE (location_type, location_external_id, updated_at)
            ON TABLE public.user_operational_preferences TO {RUNTIME_ROLE};
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.user_operational_preferences")
