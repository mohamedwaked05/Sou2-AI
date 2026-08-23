"""RQ entry points for isolated, one-at-a-time knowledge processing."""

from __future__ import annotations

import uuid
from datetime import timedelta

from redis import Redis
from rq import Queue, Retry
from rq.job import JobStatus
from sqlalchemy import delete, select

from app.core.config import Settings, get_settings
from app.core.security import utc_now
from app.database.models import (
    Business,
    BusinessStatus,
    KnowledgeDocument,
    KnowledgeDocumentChunk,
    KnowledgeDocumentStatus,
)
from app.database.session import get_session_factory
from app.rag.document_processing import (
    DocumentProcessingError,
    chunk_text,
    validate_and_extract,
)
from app.rag.embeddings import (
    EmbeddingProviderError,
    create_embedding_provider,
    embed_batched,
)
from app.storage.knowledge import get_knowledge_storage

PERMANENT = {
    "unsupported_document_type",
    "document_mime_mismatch",
    "document_content_mismatch",
    "empty_document",
    "malformed_document",
    "encrypted_document",
    "pdf_page_limit_exceeded",
    "scanned_pdf",
    "document_resource_limit",
    "empty_extracted_text",
    "extracted_text_limit_exceeded",
    "chunk_limit_exceeded",
    "embedding_model_missing",
    "embedding_invalid_response",
    "embedding_http_error",
    "embedding_output_count",
    "embedding_dimension",
    "embedding_invalid_values",
}


def document_job_id(document_id: uuid.UUID) -> str:
    """Return the non-secret, deterministic RQ identity for one document."""
    return f"knowledge-document-{document_id}"


def enqueue_document(document_id: uuid.UUID, settings: Settings) -> None:
    queue = Queue(
        settings.knowledge_queue_name, connection=Redis.from_url(settings.redis_url)
    )
    job_id = document_job_id(document_id)
    existing = queue.fetch_job(job_id)
    active_statuses = {
        JobStatus.QUEUED,
        JobStatus.STARTED,
        JobStatus.DEFERRED,
        JobStatus.SCHEDULED,
    }
    if existing is not None:
        if existing.get_status(refresh=True) in active_statuses:
            return
        existing.delete()
    queue.enqueue(
        process_document,
        str(document_id),
        job_id=job_id,
        job_timeout=settings.knowledge_worker_timeout_seconds,
        retry=Retry(max=2, interval=[2, 8]),
    )


def process_document(document_id: str) -> None:
    settings = get_settings()
    identifier = uuid.UUID(document_id)
    with get_session_factory()() as session:
        document = session.scalar(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.id == identifier)
            .with_for_update(skip_locked=True)
        )
        if document is None:
            return
        if (
            document.status is KnowledgeDocumentStatus.PROCESSING
            and document.processing_started_at is not None
            and document.processing_started_at
            <= utc_now() - timedelta(seconds=settings.knowledge_worker_timeout_seconds)
        ):
            if document.processing_attempts >= 3:
                document.status = KnowledgeDocumentStatus.FAILED
                document.failure_code = "processing_timeout"
                document.processing_completed_at = utc_now()
                session.commit()
                return
            else:
                session.execute(
                    delete(KnowledgeDocumentChunk).where(
                        KnowledgeDocumentChunk.document_id == identifier
                    )
                )
                document.status = KnowledgeDocumentStatus.PENDING
                document.processing_started_at = None
            session.flush()
        if document.status is not KnowledgeDocumentStatus.PENDING:
            return
        business = session.get(Business, document.business_id)
        if business is None or business.status is not BusinessStatus.ACTIVE:
            document.status = KnowledgeDocumentStatus.FAILED
            document.failure_code = "business_not_active"
            document.processing_started_at = utc_now()
            document.processing_completed_at = utc_now()
            session.commit()
            return
        document.status = KnowledgeDocumentStatus.PROCESSING
        document.processing_started_at = utc_now()
        document.processing_attempts += 1
        session.commit()
    try:
        storage = get_knowledge_storage(settings)
        with storage.open(
            document.business_id, document.id, document.storage_key
        ) as source:
            extracted = validate_and_extract(
                source.read(),
                document.original_filename,
                document.mime_type,
                settings.knowledge_max_pdf_pages,
                settings.knowledge_max_text_characters,
            )
        pieces = chunk_text(extracted.text)
        if len(pieces) > settings.knowledge_max_chunks:
            raise DocumentProcessingError("chunk_limit_exceeded")
        embeddings = embed_batched(
            create_embedding_provider(settings), pieces, settings.embedding_batch_size
        )
        with get_session_factory()() as session:
            current = session.scalar(
                select(KnowledgeDocument)
                .where(KnowledgeDocument.id == identifier)
                .with_for_update()
            )
            if (
                current is None
                or current.status is not KnowledgeDocumentStatus.PROCESSING
            ):
                return
            business = session.get(Business, current.business_id)
            if business is None or business.status is not BusinessStatus.ACTIVE:
                session.execute(
                    delete(KnowledgeDocumentChunk).where(
                        KnowledgeDocumentChunk.document_id == identifier
                    )
                )
                current.status = KnowledgeDocumentStatus.FAILED
                current.failure_code = "business_not_active"
                current.processing_completed_at = utc_now()
                session.commit()
                return
            session.execute(
                delete(KnowledgeDocumentChunk).where(
                    KnowledgeDocumentChunk.document_id == identifier
                )
            )
            persisted_chunks = [
                KnowledgeDocumentChunk(
                    business_id=current.business_id,
                    document_id=current.id,
                    chunk_index=index,
                    content=piece,
                    character_count=len(piece),
                    embedding=embeddings[index],
                    embedding_model=settings.embedding_model,
                    embedded_at=utc_now(),
                )
                for index, piece in enumerate(pieces)
            ]
            session.add_all(persisted_chunks)
            # The READY trigger queries the chunk table during the status UPDATE.
            # Flush this exact replacement set first so the trigger can observe it.
            session.flush(persisted_chunks)
            current.page_count = extracted.page_count
            current.status = KnowledgeDocumentStatus.READY
            current.processing_completed_at = utc_now()
            session.commit()
    except DocumentProcessingError as exc:
        _fail(identifier, exc.code)
    except EmbeddingProviderError as exc:
        retry = _fail(identifier, exc.code)
        if retry and exc.retryable:
            raise
    except Exception:
        retry = _fail(identifier, "processing_unavailable")
        if retry:
            raise


def _fail(identifier: uuid.UUID, code: str) -> bool:
    with get_session_factory()() as session:
        document = session.scalar(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.id == identifier)
            .with_for_update()
        )
        if (
            document is None
            or document.status is not KnowledgeDocumentStatus.PROCESSING
        ):
            return False
        session.execute(
            delete(KnowledgeDocumentChunk).where(
                KnowledgeDocumentChunk.document_id == identifier
            )
        )
        retry = code not in PERMANENT and document.processing_attempts < 3
        if retry:
            document.status = KnowledgeDocumentStatus.PENDING
            document.processing_started_at = None
            document.processing_completed_at = None
        else:
            document.status = KnowledgeDocumentStatus.FAILED
            document.failure_code = code
            document.processing_completed_at = utc_now()
        session.commit()
        return retry
