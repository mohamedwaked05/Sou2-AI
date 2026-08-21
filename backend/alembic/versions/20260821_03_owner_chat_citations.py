"""Persist safe owner-chat source citations.

Revision ID: 20260821_03
Revises: 20260821_02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260821_03"
down_revision: str | None = "20260821_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE public.owner_chat_citations (
      id uuid PRIMARY KEY, business_id uuid NOT NULL REFERENCES public.businesses(id) ON DELETE CASCADE,
      assistant_message_id uuid NOT NULL REFERENCES public.owner_chat_messages(id) ON DELETE CASCADE,
      document_id uuid REFERENCES public.knowledge_documents(id) ON DELETE SET NULL,
      chunk_id uuid REFERENCES public.knowledge_document_chunks(id) ON DELETE SET NULL,
      citation_order integer NOT NULL CONSTRAINT ck_owner_chat_citations_order CHECK (citation_order >= 0), label varchar(20) NOT NULL CONSTRAINT ck_owner_chat_citations_label CHECK (label ~ '^S[1-9][0-9]*$'),
      filename varchar(255) NOT NULL, page_start integer, page_end integer, section_title varchar(500),
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_owner_chat_citation_order UNIQUE (assistant_message_id, citation_order),
      CONSTRAINT uq_owner_chat_citation_label UNIQUE (assistant_message_id, label)
    );
    CREATE INDEX ix_owner_chat_citations_business ON public.owner_chat_citations (business_id, assistant_message_id);
    CREATE FUNCTION public.sou2ai_guard_owner_chat_citation() RETURNS trigger AS $function$
    BEGIN
      -- A document deletion first SET NULLs document_id and then removes its
      -- chunks. Permit only that FK transition; ordinary rows remain scoped.
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
    CREATE TRIGGER trg_owner_chat_citations_scope BEFORE INSERT OR UPDATE ON public.owner_chat_citations FOR EACH ROW EXECUTE FUNCTION public.sou2ai_guard_owner_chat_citation();
    """)
    op.execute("ALTER TABLE public.owner_chat_citations OWNER TO sou2ai_migrator")
    op.execute(
        "REVOKE ALL ON TABLE public.owner_chat_citations FROM PUBLIC, sou2ai_lifecycle_operator"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.owner_chat_citations TO sou2ai_runtime"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_owner_chat_citations_scope ON public.owner_chat_citations"
    )
    op.execute("DROP FUNCTION IF EXISTS public.sou2ai_guard_owner_chat_citation()")
    op.execute("DROP TABLE public.owner_chat_citations")
