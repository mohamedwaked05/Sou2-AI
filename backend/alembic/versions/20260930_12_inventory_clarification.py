"""Keep bounded inventory clarification context on its originating turn."""

from alembic import op

revision = "20260930_12"
down_revision = "20260830_11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.owner_chat_messages "
        "ADD COLUMN operational_clarification jsonb "
        "CHECK (operational_clarification IS NULL OR "
        "(role = 'owner' AND jsonb_typeof(operational_clarification) = 'object' "
        "AND octet_length(operational_clarification::text) <= 8192))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE public.owner_chat_messages DROP COLUMN operational_clarification"
    )
