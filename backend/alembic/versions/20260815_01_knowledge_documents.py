"""Add pgvector-backed tenant document metadata and chunks.

Revision ID: 20260815_01
Revises: 20260813_04
Create Date: 2026-08-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260815_01"
down_revision: str | None = "20260813_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATOR_ROLE = "sou2ai_migrator"
RUNTIME_ROLE = "sou2ai_runtime"
OPERATOR_ROLE = "sou2ai_lifecycle_operator"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        "CREATE TYPE public.knowledge_document_status AS ENUM "
        "('PENDING', 'PROCESSING', 'READY', 'FAILED')"
    )
    op.execute(
        """
        CREATE TABLE public.knowledge_documents (
            id uuid PRIMARY KEY,
            business_id uuid NOT NULL REFERENCES public.businesses(id) ON DELETE CASCADE,
            uploaded_by_user_id uuid REFERENCES public.users(id) ON DELETE SET NULL,
            original_filename varchar(255) NOT NULL,
            mime_type varchar(255) NOT NULL,
            file_size_bytes bigint NOT NULL,
            content_sha256 varchar(64) NOT NULL,
            storage_key varchar(1024) NOT NULL,
            status public.knowledge_document_status NOT NULL DEFAULT 'PENDING',
            failure_code varchar(100),
            failure_message varchar(1000),
            page_count integer,
            replaces_document_id uuid,
            processing_started_at timestamptz,
            processing_completed_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_knowledge_documents_business_hash UNIQUE (business_id, content_sha256),
            CONSTRAINT uq_knowledge_documents_id_business UNIQUE (id, business_id),
            CONSTRAINT fk_knowledge_documents_replacement_same_business
                FOREIGN KEY (replaces_document_id, business_id)
                REFERENCES public.knowledge_documents(id, business_id) ON DELETE RESTRICT,
            CONSTRAINT ck_knowledge_documents_filename CHECK (char_length(btrim(original_filename)) BETWEEN 1 AND 255),
            CONSTRAINT ck_knowledge_documents_filename_safe CHECK (original_filename !~ '[/\\\\[:cntrl:]]'),
            CONSTRAINT ck_knowledge_documents_mime_type CHECK (char_length(btrim(mime_type)) BETWEEN 1 AND 255),
            CONSTRAINT ck_knowledge_documents_file_size CHECK (file_size_bytes > 0),
            CONSTRAINT ck_knowledge_documents_hash CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_knowledge_documents_storage_key CHECK (char_length(btrim(storage_key)) BETWEEN 1 AND 1024 AND storage_key !~ '(^/|^[A-Za-z]:[/\\\\]|^https?://|(^|/)\\.\\.(/|$)|[[:cntrl:]])'),
            CONSTRAINT ck_knowledge_documents_page_count CHECK (page_count IS NULL OR page_count > 0),
            CONSTRAINT ck_knowledge_documents_not_self_replacement CHECK (replaces_document_id IS NULL OR replaces_document_id <> id),
            CONSTRAINT ck_knowledge_documents_failure_code CHECK (failure_code IS NULL OR (char_length(failure_code) BETWEEN 1 AND 100 AND failure_code ~ '^[a-z][a-z0-9_]*(\\.[a-z0-9_]+)*$')),
            CONSTRAINT ck_knowledge_documents_failure_message CHECK (failure_message IS NULL OR (char_length(btrim(failure_message)) BETWEEN 1 AND 1000 AND failure_message !~ '[[:cntrl:]]')),
            CONSTRAINT ck_knowledge_documents_processing_metadata CHECK (
                (status = 'PENDING' AND processing_started_at IS NULL AND processing_completed_at IS NULL AND failure_code IS NULL AND failure_message IS NULL)
                OR (status = 'PROCESSING' AND processing_started_at IS NOT NULL AND processing_completed_at IS NULL AND failure_code IS NULL AND failure_message IS NULL)
                OR (status = 'READY' AND processing_started_at IS NOT NULL AND processing_completed_at IS NOT NULL AND failure_code IS NULL AND failure_message IS NULL)
                OR (status = 'FAILED' AND processing_started_at IS NOT NULL AND processing_completed_at IS NOT NULL AND failure_code IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.knowledge_document_chunks (
            id uuid PRIMARY KEY,
            business_id uuid NOT NULL,
            document_id uuid NOT NULL,
            chunk_index integer NOT NULL,
            content text NOT NULL,
            page_start integer,
            page_end integer,
            section_title varchar(500),
            character_count integer NOT NULL,
            embedding vector(1024),
            embedding_model varchar(255),
            embedded_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_knowledge_document_chunks_order UNIQUE (document_id, chunk_index),
            CONSTRAINT fk_knowledge_document_chunks_document_same_business
                FOREIGN KEY (document_id, business_id)
                REFERENCES public.knowledge_documents(id, business_id) ON DELETE CASCADE,
            CONSTRAINT ck_knowledge_document_chunks_index CHECK (chunk_index >= 0),
            CONSTRAINT ck_knowledge_document_chunks_content CHECK (char_length(btrim(content)) BETWEEN 1 AND 100000),
            CONSTRAINT ck_knowledge_document_chunks_character_count CHECK (character_count > 0 AND character_count = char_length(content)),
            CONSTRAINT ck_knowledge_document_chunks_pages CHECK ((page_start IS NULL AND page_end IS NULL) OR (page_start > 0 AND page_end >= page_start)),
            CONSTRAINT ck_knowledge_document_chunks_section CHECK (section_title IS NULL OR char_length(btrim(section_title)) BETWEEN 1 AND 500),
            CONSTRAINT ck_knowledge_document_chunks_embedding_metadata CHECK ((embedding IS NULL AND embedding_model IS NULL AND embedded_at IS NULL) OR (embedding IS NOT NULL AND char_length(btrim(embedding_model)) BETWEEN 1 AND 255 AND embedded_at IS NOT NULL))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_knowledge_documents_business_status_created ON public.knowledge_documents (business_id, status, created_at, id)"
    )
    op.execute(
        "CREATE INDEX ix_knowledge_documents_business_updated ON public.knowledge_documents (business_id, updated_at, id)"
    )
    op.execute(
        "CREATE INDEX ix_knowledge_documents_replaces ON public.knowledge_documents (replaces_document_id)"
    )
    op.execute(
        "CREATE INDEX ix_knowledge_document_chunks_business_document_order ON public.knowledge_document_chunks (business_id, document_id, chunk_index)"
    )
    op.execute(
        """
        CREATE FUNCTION public.sou2ai_guard_knowledge_document_transition()
        RETURNS trigger AS $function$
        BEGIN
            IF TG_OP = 'UPDATE' AND NEW.status IS DISTINCT FROM OLD.status AND NOT (
                (OLD.status = 'PENDING' AND NEW.status = 'PROCESSING') OR
                (OLD.status = 'PROCESSING' AND NEW.status IN ('READY', 'FAILED')) OR
                (OLD.status = 'FAILED' AND NEW.status = 'PENDING')
            ) THEN
                RAISE EXCEPTION 'Knowledge document transition is not allowed.' USING ERRCODE = '23514';
            END IF;
            IF NEW.status = 'READY' AND NOT EXISTS (
                SELECT 1 FROM public.knowledge_document_chunks WHERE document_id = NEW.id
            ) THEN
                RAISE EXCEPTION 'Ready knowledge documents require a chunk.' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $function$ LANGUAGE plpgsql SET search_path = pg_catalog, public;
        CREATE TRIGGER trg_knowledge_documents_transition
        BEFORE INSERT OR UPDATE ON public.knowledge_documents
        FOR EACH ROW EXECUTE FUNCTION public.sou2ai_guard_knowledge_document_transition();

        CREATE FUNCTION public.sou2ai_guard_ready_document_chunk_delete()
        RETURNS trigger AS $function$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.knowledge_documents WHERE id = OLD.document_id AND status = 'READY')
               AND NOT EXISTS (SELECT 1 FROM public.knowledge_document_chunks WHERE document_id = OLD.document_id AND id <> OLD.id) THEN
                RAISE EXCEPTION 'Ready knowledge documents require a chunk.' USING ERRCODE = '23514';
            END IF;
            RETURN OLD;
        END;
        $function$ LANGUAGE plpgsql SET search_path = pg_catalog, public;
        CREATE TRIGGER trg_knowledge_document_chunks_ready_delete
        BEFORE DELETE ON public.knowledge_document_chunks
        FOR EACH ROW EXECUTE FUNCTION public.sou2ai_guard_ready_document_chunk_delete();
        """
    )
    for table in ("knowledge_documents", "knowledge_document_chunks"):
        op.execute(f"ALTER TABLE public.{table} OWNER TO {MIGRATOR_ROLE}")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, {OPERATOR_ROLE}")
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.{table} TO {RUNTIME_ROLE}"
        )
    op.execute(f"ALTER TYPE public.knowledge_document_status OWNER TO {MIGRATOR_ROLE}")
    op.execute(
        f"GRANT USAGE ON TYPE public.knowledge_document_status TO {RUNTIME_ROLE}"
    )
    for function in (
        "sou2ai_guard_knowledge_document_transition()",
        "sou2ai_guard_ready_document_chunk_delete()",
    ):
        op.execute(f"ALTER FUNCTION public.{function} OWNER TO {MIGRATOR_ROLE}")
        op.execute(
            f"REVOKE ALL ON FUNCTION public.{function} FROM PUBLIC, {RUNTIME_ROLE}, {OPERATOR_ROLE}"
        )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_knowledge_document_chunks_ready_delete ON public.knowledge_document_chunks"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_knowledge_documents_transition ON public.knowledge_documents"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.sou2ai_guard_ready_document_chunk_delete()"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.sou2ai_guard_knowledge_document_transition()"
    )
    op.execute("DROP TABLE IF EXISTS public.knowledge_document_chunks")
    op.execute("DROP TABLE IF EXISTS public.knowledge_documents")
    op.execute("DROP TYPE IF EXISTS public.knowledge_document_status")
    # Intentionally retain vector: other migrations or application objects may use it.
