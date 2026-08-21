"""Harden document replacement and retry transitions.

Revision ID: 20260821_02
Revises: 20260821_01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260821_02"
down_revision: str | None = "20260821_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX uq_knowledge_documents_single_replacement ON public.knowledge_documents (replaces_document_id) WHERE replaces_document_id IS NOT NULL"
    )
    op.execute("""
    CREATE OR REPLACE FUNCTION public.sou2ai_guard_knowledge_document_transition()
    RETURNS trigger AS $function$
    BEGIN
      IF TG_OP = 'UPDATE' AND NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'PENDING' AND NEW.status IN ('PROCESSING', 'FAILED')) OR
        (OLD.status = 'PROCESSING' AND NEW.status IN ('PENDING', 'READY', 'FAILED')) OR
        (OLD.status = 'FAILED' AND NEW.status = 'PENDING')
      ) THEN RAISE EXCEPTION 'Knowledge document transition is not allowed.' USING ERRCODE = '23514'; END IF;
      IF NEW.status = 'READY' AND NOT EXISTS (SELECT 1 FROM public.knowledge_document_chunks WHERE document_id = NEW.id) THEN RAISE EXCEPTION 'Ready knowledge documents require a chunk.' USING ERRCODE = '23514'; END IF;
      RETURN NEW;
    END; $function$ LANGUAGE plpgsql SET search_path = pg_catalog, public;
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.uq_knowledge_documents_single_replacement")
