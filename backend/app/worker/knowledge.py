"""RQ entry points for isolated, one-at-a-time knowledge processing."""

from __future__ import annotations

import uuid

from redis import Redis
from rq import Queue, Retry
from sqlalchemy import delete, select

from app.core.config import Settings, get_settings
from app.core.security import utc_now
from app.database.models import (
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
from app.storage.knowledge import LocalKnowledgeStorage

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
}


def enqueue_document(document_id: uuid.UUID, settings: Settings) -> None:
    queue = Queue(
        settings.knowledge_queue_name, connection=Redis.from_url(settings.redis_url)
    )
    queue.enqueue(
        process_document,
        str(document_id),
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
        if document is None or document.status is not KnowledgeDocumentStatus.PENDING:
            return
        document.status = KnowledgeDocumentStatus.PROCESSING
        document.processing_started_at = utc_now()
        document.processing_attempts += 1
        session.commit()
    try:
        storage = LocalKnowledgeStorage(settings.knowledge_storage_root)
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
            session.execute(
                delete(KnowledgeDocumentChunk).where(
                    KnowledgeDocumentChunk.document_id == identifier
                )
            )
            session.add_all(
                KnowledgeDocumentChunk(
                    business_id=current.business_id,
                    document_id=current.id,
                    chunk_index=index,
                    content=piece,
                    character_count=len(piece),
                )
                for index, piece in enumerate(pieces)
            )
            current.page_count = extracted.page_count
            current.status = KnowledgeDocumentStatus.READY
            current.processing_completed_at = utc_now()
            session.commit()
    except DocumentProcessingError as exc:
        _fail(identifier, exc.code)
    except Exception:
        _fail(identifier, "processing_unavailable")
        raise


def _fail(identifier: uuid.UUID, code: str) -> None:
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
            return
        session.execute(
            delete(KnowledgeDocumentChunk).where(
                KnowledgeDocumentChunk.document_id == identifier
            )
        )
        document.status = KnowledgeDocumentStatus.FAILED
        document.failure_code = code
        document.processing_completed_at = utc_now()
        session.commit()
