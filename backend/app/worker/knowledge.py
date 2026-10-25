"""RQ entry points for isolated, one-at-a-time knowledge processing."""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from redis import Redis
from rq import Queue, Retry, get_current_job
from rq.exceptions import DuplicateJobError
from rq.job import JobStatus
from rq.timeouts import JobTimeoutException
from sqlalchemy import delete, select

from app.core.admission import require_open_admission
from app.core.config import Settings, get_settings
from app.core.runtime import (
    job_contract,
    preserve_incompatible_failed_job,
    validate_worker_job,
)
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


def enqueue_document(
    document_id: uuid.UUID, settings: Settings, *, retry_failed: bool = False
) -> None:
    enqueue_knowledge_job(
        document_id,
        settings,
        process_document,
        document_job_id(document_id),
        reviewed_retry=retry_failed,
        on_failure=document_job_failed,
    )


def enqueue_knowledge_job(
    document_id: uuid.UUID,
    settings: Settings,
    function: Callable[[str], None],
    job_id: str,
    *,
    reviewed_retry: bool = False,
    on_failure: Callable | None = None,
) -> None:
    """Atomically deduplicate work while retaining every terminal job record."""
    require_open_admission(settings)
    queue = Queue(
        settings.knowledge_queue_name, connection=Redis.from_url(settings.redis_url)
    )
    base_job_id = job_id
    active_statuses = {
        JobStatus.QUEUED,
        JobStatus.STARTED,
        JobStatus.DEFERRED,
        JobStatus.SCHEDULED,
    }
    for _ in range(100):
        existing = queue.fetch_job(job_id)
        if existing is None:
            break
        if existing.get_status(refresh=True) in active_statuses:
            return
        if not reviewed_retry:
            preserve_incompatible_failed_job(existing, settings, "knowledge")
        # Each retained terminal attempt has one deterministic successor. Two
        # simultaneous retry requests therefore choose the same unique RQ job.
        suffix = hashlib.sha256(job_id.encode()).hexdigest()[:24]
        job_id = f"{base_job_id}-retry-{suffix}"
    else:
        raise RuntimeError("knowledge_retry_history_review_required")
    try:
        queue.enqueue(
            function,
            str(document_id),
            job_id=job_id,
            job_timeout=settings.knowledge_worker_timeout_seconds,
            retry=Retry(max=2, interval=[2, 8]),
            on_failure=on_failure,
            unique=True,
            meta={"runtime_contract": job_contract(settings, "knowledge")},
        )
    except DuplicateJobError:
        return


def process_document(document_id: str) -> None:
    settings = get_settings()
    validate_worker_job(settings, "knowledge")
    clear_embedding_failure()
    identifier = uuid.UUID(document_id)
    deadline = time.monotonic() + settings.knowledge_worker_timeout_seconds - 30
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
        claim = (document.processing_started_at, document.processing_attempts)
        job = get_current_job()
        if job is not None:
            job.meta["document_attempt_started_at"] = claim[0].isoformat()
            job.meta["document_attempt"] = claim[1]
            job.save_meta()
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
            create_embedding_provider(settings),
            pieces,
            settings.embedding_batch_size,
            deadline=deadline,
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
                or (current.processing_started_at, current.processing_attempts) != claim
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
        _fail(identifier, exc.code, claim=claim)
    except EmbeddingProviderError as exc:
        record_embedding_failure(exc)
        retry = _fail(identifier, exc.code, retryable=exc.retryable, claim=claim)
        if retry and exc.retryable:
            raise
    except JobTimeoutException:
        if _fail(identifier, "processing_timeout", retryable=True, claim=claim):
            raise
    except Exception:
        retry = _fail(identifier, "processing_unavailable", claim=claim)
        if retry:
            raise


def _fail(
    identifier: uuid.UUID,
    code: str,
    *,
    retryable: bool | None = None,
    claim: tuple[datetime, int] | None = None,
) -> bool:
    with get_session_factory()() as session:
        document = session.scalar(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.id == identifier)
            .with_for_update()
        )
        if (
            document is None
            or document.status is not KnowledgeDocumentStatus.PROCESSING
            or (
                claim is not None
                and (document.processing_started_at, document.processing_attempts)
                != claim
            )
        ):
            return False
        session.execute(
            delete(KnowledgeDocumentChunk).where(
                KnowledgeDocumentChunk.document_id == identifier
            )
        )
        retry = (
            retryable if retryable is not None else code not in PERMANENT
        ) and document.processing_attempts < 3
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


def record_embedding_failure(error: EmbeddingProviderError) -> None:
    job = get_current_job()
    if job is None:
        return
    job.meta["embedding_failure_code"] = error.code
    job.meta["embedding_failure_retryable"] = error.retryable
    job.meta.pop("embedding_http_status", None)
    job.meta.pop("embedding_retry_after_seconds", None)
    if error.http_status is not None:
        job.meta["embedding_http_status"] = error.http_status
    if error.retry_after_seconds is not None:
        job.meta["embedding_retry_after_seconds"] = error.retry_after_seconds
    job.save_meta()


def clear_embedding_failure() -> None:
    """A retry must not inherit the previous attempt's delay or failure class."""
    job = get_current_job()
    if job is None:
        return
    keys = (
        "embedding_failure_code",
        "embedding_failure_retryable",
        "embedding_http_status",
        "embedding_retry_after_seconds",
    )
    if any(key in job.meta for key in keys):
        for key in keys:
            job.meta.pop(key, None)
        job.save_meta()


def document_job_failed(job, connection, exception_type, exception, traceback) -> None:
    """RQ invokes this even when the work process times out or is abandoned."""
    started = job.meta.get("document_attempt_started_at")
    attempt = job.meta.get("document_attempt")
    if not isinstance(started, str) or not isinstance(attempt, int):
        return
    _fail(
        uuid.UUID(job.args[0]),
        "processing_timeout",
        retryable=bool(job.retries_left),
        claim=(datetime.fromisoformat(started), attempt),
    )


def recover_stale_documents(settings: Settings, *, batch_size: int = 25) -> int:
    """Bounded recovery for interrupted jobs whose callback could not run."""
    require_open_admission(settings)
    cutoff = utc_now() - timedelta(
        seconds=settings.knowledge_worker_timeout_seconds + 30
    )
    with get_session_factory()() as session:
        documents = session.scalars(
            select(KnowledgeDocument)
            .where(
                KnowledgeDocument.status == KnowledgeDocumentStatus.PROCESSING,
                KnowledgeDocument.processing_started_at < cutoff,
            )
            .order_by(KnowledgeDocument.processing_started_at, KnowledgeDocument.id)
            .limit(min(max(batch_size, 1), 100))
            .with_for_update(skip_locked=True)
        ).all()
        for document in documents:
            document.status = KnowledgeDocumentStatus.FAILED
            document.failure_code = "processing_timeout"
            document.processing_completed_at = utc_now()
        session.commit()
        return len(documents)
