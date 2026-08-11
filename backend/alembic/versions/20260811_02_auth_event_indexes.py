"""Add authentication-event query and retention indexes.

Revision ID: 20260811_02
Revises: 20260811_01
Create Date: 2026-08-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260811_02"
down_revision: str | None = "20260811_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_auth_events_type_email_created",
        "authentication_events",
        ["event_type", "normalized_email", "created_at"],
    )
    op.create_index(
        "ix_auth_events_type_ip_created",
        "authentication_events",
        ["event_type", "client_ip", "created_at"],
    )
    op.create_index("ix_auth_events_created", "authentication_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_auth_events_created", table_name="authentication_events")
    op.drop_index("ix_auth_events_type_ip_created", table_name="authentication_events")
    op.drop_index(
        "ix_auth_events_type_email_created", table_name="authentication_events"
    )
