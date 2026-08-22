"""Persist the selected business language for onboarding completion.

Revision ID: 20260822_05
Revises: 20260822_04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260822_05"
down_revision: str | None = "20260822_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column(
            "default_language",
            postgresql.ENUM("ar", "en", name="default_language", create_type=False),
            nullable=True,
        ),
    )
    op.execute("GRANT UPDATE (default_language) ON TABLE public.businesses TO sou2ai_runtime")


def downgrade() -> None:
    op.execute("REVOKE UPDATE (default_language) ON TABLE public.businesses FROM sou2ai_runtime")
    op.drop_column("businesses", "default_language")
