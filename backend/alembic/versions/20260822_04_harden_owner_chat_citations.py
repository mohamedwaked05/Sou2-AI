"""Harden owner-chat citation trigger integrity.

Revision ID: 20260822_04
Revises: 20260821_03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260822_04"
down_revision: str | None = "20260821_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
    CREATE OR REPLACE FUNCTION public.sou2ai_guard_owner_chat_citation()
    RETURNS trigger AS $function$
    BEGIN
      IF TG_OP = 'UPDATE'
         AND pg_trigger_depth() > 1
         AND OLD.document_id IS NOT NULL
         AND NEW.document_id IS NULL
         AND NEW.business_id IS NOT DISTINCT FROM OLD.business_id
         AND NEW.assistant_message_id IS NOT DISTINCT FROM OLD.assistant_message_id
         AND NEW.chunk_id IS NOT DISTINCT FROM OLD.chunk_id
         AND NEW.citation_order IS NOT DISTINCT FROM OLD.citation_order
         AND NEW.label IS NOT DISTINCT FROM OLD.label
         AND NEW.filename IS NOT DISTINCT FROM OLD.filename
         AND NEW.page_start IS NOT DISTINCT FROM OLD.page_start
         AND NEW.page_end IS NOT DISTINCT FROM OLD.page_end
         AND NEW.section_title IS NOT DISTINCT FROM OLD.section_title
         AND NEW.created_at IS NOT DISTINCT FROM OLD.created_at THEN
        RETURN NEW;
      END IF;
      IF NOT EXISTS (
        SELECT 1
        FROM public.owner_chat_messages m
        JOIN public.owner_conversations c ON c.id = m.conversation_id
        WHERE m.id = NEW.assistant_message_id
          AND m.role = 'assistant'
          AND c.business_id = NEW.business_id
      ) THEN
        RAISE EXCEPTION 'Citation assistant/business scope is invalid.' USING ERRCODE = '23514';
      END IF;
      IF NEW.document_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.knowledge_documents d
        WHERE d.id = NEW.document_id AND d.business_id = NEW.business_id
      ) THEN
        RAISE EXCEPTION 'Citation document scope is invalid.' USING ERRCODE = '23514';
      END IF;
      IF NEW.chunk_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.knowledge_document_chunks k
        WHERE k.id = NEW.chunk_id
          AND k.business_id = NEW.business_id
          AND k.document_id = NEW.document_id
      ) THEN
        RAISE EXCEPTION 'Citation chunk scope is invalid.' USING ERRCODE = '23514';
      END IF;
      RETURN NEW;
    END; $function$ LANGUAGE plpgsql SET search_path = pg_catalog, public;
    """)


def downgrade() -> None:
    op.execute("""
    CREATE OR REPLACE FUNCTION public.sou2ai_guard_owner_chat_citation()
    RETURNS trigger AS $function$
    BEGIN
      IF TG_OP = 'UPDATE' AND OLD.document_id IS NOT NULL AND NEW.document_id IS NULL AND NEW.chunk_id = OLD.chunk_id THEN
        RETURN NEW;
      END IF;
      IF NOT EXISTS (SELECT 1 FROM public.owner_chat_messages m JOIN public.owner_conversations c ON c.id=m.conversation_id WHERE m.id=NEW.assistant_message_id AND m.role='assistant' AND c.business_id=NEW.business_id) THEN
        RAISE EXCEPTION 'Citation assistant/business scope is invalid.' USING ERRCODE='23514';
      END IF;
      IF NEW.document_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM public.knowledge_documents d WHERE d.id=NEW.document_id AND d.business_id=NEW.business_id) THEN
        RAISE EXCEPTION 'Citation document scope is invalid.' USING ERRCODE='23514';
      END IF;
      IF NEW.chunk_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM public.knowledge_document_chunks k WHERE k.id=NEW.chunk_id AND k.business_id=NEW.business_id) THEN
        RAISE EXCEPTION 'Citation chunk scope is invalid.' USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END; $function$ LANGUAGE plpgsql SET search_path=pg_catalog,public;
    """)
