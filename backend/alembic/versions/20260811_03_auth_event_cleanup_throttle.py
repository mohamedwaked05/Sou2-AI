"""Add persistent authentication-event cleanup throttle.

Revision ID: 20260811_03
Revises: 20260811_02
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260811_03"
down_revision: str | None = "20260811_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "authentication_maintenance_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_name", sa.String(100), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "btrim(task_name) <> ''",
            name="ck_auth_maintenance_task_name_not_blank",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_name"),
    )


def downgrade() -> None:
    op.drop_table("authentication_maintenance_tasks")
