"""Apply the approved owner-message content limit.

Revision ID: 20260812_03
Revises: 20260812_02
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260812_03"
down_revision: str | None = "20260812_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_owner_chat_messages_content_length",
        "owner_chat_messages",
        type_="check",
    )
    op.create_check_constraint(
        "ck_owner_chat_messages_content_length",
        "owner_chat_messages",
        sa.text(
            "(role = 'owner' AND "
            "char_length(btrim(content)) BETWEEN 1 AND 4000) OR "
            "(role = 'assistant' AND "
            "char_length(btrim(content)) BETWEEN 1 AND 14000)"
        ),
        postgresql_not_valid=True,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_owner_chat_messages_content_length",
        "owner_chat_messages",
        type_="check",
    )
    op.create_check_constraint(
        "ck_owner_chat_messages_content_length",
        "owner_chat_messages",
        sa.text("char_length(btrim(content)) BETWEEN 1 AND 14000"),
    )
