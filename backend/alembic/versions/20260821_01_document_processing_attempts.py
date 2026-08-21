"""Track durable knowledge-document processing attempts.

Revision ID: 20260821_01
Revises: 20260815_01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260821_01"
down_revision: str | None = "20260815_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.knowledge_documents ADD COLUMN processing_attempts "
        "integer NOT NULL DEFAULT 0 CHECK (processing_attempts BETWEEN 0 AND 3)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE public.knowledge_documents DROP COLUMN processing_attempts")
