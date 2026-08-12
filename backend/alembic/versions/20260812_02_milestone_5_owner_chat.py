"""Implement Milestone 5 owner chat and learned business knowledge.

Revision ID: 20260812_02
Revises: 20260812_01
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260812_02"
down_revision: str | None = "20260812_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_active_profile_field_guard() -> None:
    op.execute(
        """
        CREATE FUNCTION sou2ai_guard_active_profile_fields() RETURNS trigger AS $$
        BEGIN
            IF NEW.is_active
               AND NOT sou2ai_business_profile_complete(NEW.id) THEN
                RAISE EXCEPTION 'An active business must retain a valid profile.'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER trg_businesses_active_profile_fields
        AFTER UPDATE OF name, description, category, custom_category,
                        governorate, district, city, address_line ON businesses
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION sou2ai_guard_active_profile_fields();
        """
    )


def upgrade() -> None:
    _create_active_profile_field_guard()

    op.create_table(
        "owner_conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "next_turn_number", sa.BigInteger(), server_default="1", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "next_turn_number > 0", name="ck_owner_conversations_next_turn_positive"
        ),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id"),
    )
    op.execute(
        "INSERT INTO owner_conversations (id, business_id) "
        "SELECT gen_random_uuid(), id FROM businesses"
    )

    op.create_table(
        "owner_chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_number", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("reply_to_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("generation_state", sa.String(length=20), nullable=True),
        sa.Column(
            "generation_claim_token", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "generation_claim_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "generation_attempts", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(btrim(content)) BETWEEN 1 AND 14000",
            name="ck_owner_chat_messages_content_length",
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'assistant')",
            name="ck_owner_chat_messages_role",
        ),
        sa.CheckConstraint(
            "generation_state IS NULL OR generation_state IN "
            "('pending', 'processing', 'completed', 'failed')",
            name="ck_owner_chat_messages_generation_state",
        ),
        sa.CheckConstraint(
            "(role = 'owner' AND sequence_number % 2 = 1 AND "
            "idempotency_key IS NOT NULL AND reply_to_message_id IS NULL AND "
            "generation_state IS NOT NULL) OR "
            "(role = 'assistant' AND sequence_number % 2 = 0 AND "
            "idempotency_key IS NULL AND reply_to_message_id IS NOT NULL AND "
            "generation_state IS NULL)",
            name="ck_owner_chat_messages_role_fields",
        ),
        sa.CheckConstraint(
            "btrim(idempotency_key) <> ''",
            name="ck_owner_chat_messages_idempotency_not_blank",
        ),
        sa.CheckConstraint(
            "(generation_state = 'processing' AND generation_claim_token IS NOT NULL "
            "AND generation_claim_expires_at IS NOT NULL) OR "
            "(generation_state IS DISTINCT FROM 'processing' AND "
            "generation_claim_token IS NULL AND generation_claim_expires_at IS NULL)",
            name="ck_owner_chat_messages_claim_state",
        ),
        sa.CheckConstraint(
            "generation_attempts >= 0",
            name="ck_owner_chat_messages_attempts_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["owner_conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id", "sequence_number", name="uq_owner_chat_message_order"
        ),
        sa.UniqueConstraint(
            "conversation_id", "idempotency_key", name="uq_owner_chat_idempotency"
        ),
        sa.UniqueConstraint(
            "conversation_id", "id", name="uq_owner_chat_message_conversation_id"
        ),
    )
    op.create_foreign_key(
        "fk_owner_chat_reply_same_conversation",
        "owner_chat_messages",
        "owner_chat_messages",
        ["conversation_id", "reply_to_message_id"],
        ["conversation_id", "id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_owner_chat_reply", "owner_chat_messages", ["reply_to_message_id"]
    )
    op.create_index(
        "ix_owner_chat_messages_history",
        "owner_chat_messages",
        ["conversation_id", sa.text("sequence_number DESC"), "id"],
    )
    op.create_index(
        "ix_owner_chat_messages_generation",
        "owner_chat_messages",
        ["conversation_id", "generation_state", "sequence_number"],
    )

    op.create_table(
        "business_knowledge",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_key", sa.String(length=100), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "source", sa.String(length=30), server_default="owner_chat", nullable=False
        ),
        sa.Column("source_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "subject_key ~ '^[a-z0-9]+(_[a-z0-9]+)*$'",
            name="ck_business_knowledge_subject_key",
        ),
        sa.CheckConstraint(
            "char_length(btrim(content)) BETWEEN 1 AND 4000",
            name="ck_business_knowledge_content_length",
        ),
        sa.CheckConstraint(
            "kind IN ('permanent', 'temporary')",
            name="ck_business_knowledge_kind",
        ),
        sa.CheckConstraint(
            "category IN ('delivery', 'returns', 'warranty', 'service', 'policy', "
            "'temporary_notice', 'promotion')",
            name="ck_business_knowledge_category",
        ),
        sa.CheckConstraint(
            "(kind = 'permanent' AND expires_at IS NULL) OR "
            "(kind = 'temporary' AND expires_at IS NOT NULL)",
            name="ck_business_knowledge_expiry",
        ),
        sa.CheckConstraint(
            "source = 'owner_chat'", name="ck_business_knowledge_source"
        ),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_message_id"], ["owner_chat_messages.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "business_id", "subject_key", name="uq_business_knowledge_subject"
        ),
    )
    op.create_index(
        "ix_business_knowledge_context",
        "business_knowledge",
        ["business_id", "expires_at"],
    )
    op.create_index(
        "ix_business_knowledge_management",
        "business_knowledge",
        ["business_id", "updated_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_business_knowledge_management", table_name="business_knowledge")
    op.drop_index("ix_business_knowledge_context", table_name="business_knowledge")
    op.drop_table("business_knowledge")
    op.drop_index("ix_owner_chat_messages_generation", table_name="owner_chat_messages")
    op.drop_index("ix_owner_chat_messages_history", table_name="owner_chat_messages")
    op.drop_table("owner_chat_messages")
    op.drop_table("owner_conversations")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_businesses_active_profile_fields ON businesses"
    )
    op.execute("DROP FUNCTION IF EXISTS sou2ai_guard_active_profile_fields()")
