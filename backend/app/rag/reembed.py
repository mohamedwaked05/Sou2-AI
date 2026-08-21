"""Internal queue and CLI support for safe whole-document re-embedding."""

from __future__ import annotations

import argparse
import uuid

from redis import Redis
from rq import Queue, Retry
from rq.job import JobStatus
from sqlalchemy import or_, select

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
from app.rag.embeddings import (
    EmbeddingProviderError,
    create_embedding_provider,
    embed_batched,
)


def reembed_job_id(document_id: uuid.UUID, model: str) -> str:
    return f"knowledge-reembed-{document_id}-{model.replace(':', '-')}"


def enqueue_reembed(document_id: uuid.UUID, settings: Settings) -> None:
    queue = Queue(
        settings.knowledge_queue_name, connection=Redis.from_url(settings.redis_url)
    )
    job_id = reembed_job_id(document_id, settings.embedding_model)
    existing = queue.fetch_job(job_id)
    if existing and existing.get_status(refresh=True) in {
        JobStatus.QUEUED,
        JobStatus.STARTED,
        JobStatus.DEFERRED,
        JobStatus.SCHEDULED,
    }:
        return
    if existing:
        existing.delete()
    queue.enqueue(
        process_reembed,
        str(document_id),
        job_id=job_id,
        job_timeout=settings.knowledge_worker_timeout_seconds,
        retry=Retry(max=2, interval=[2, 8]),
    )


def enqueue_needed(business_id: uuid.UUID | None, settings: Settings) -> int:
    with get_session_factory()() as session:
        query = (
            select(KnowledgeDocument.id)
            .join(KnowledgeDocumentChunk)
            .join(Business, Business.id == KnowledgeDocument.business_id)
            .where(
                KnowledgeDocument.status == KnowledgeDocumentStatus.READY,
                Business.status == BusinessStatus.ACTIVE,
                or_(
                    KnowledgeDocumentChunk.embedding.is_(None),
                    KnowledgeDocumentChunk.embedding_model != settings.embedding_model,
                ),
            )
            .distinct()
        )
        if business_id is not None:
            query = query.where(KnowledgeDocument.business_id == business_id)
        identifiers = session.scalars(query).all()
    for identifier in identifiers:
        enqueue_reembed(identifier, settings)
    return len(identifiers)


def process_reembed(document_id: str) -> None:
    settings = get_settings()
    identifier = uuid.UUID(document_id)
    with get_session_factory()() as session:
        document = session.scalar(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.id == identifier)
            .with_for_update()
        )
        if document is None or document.status is not KnowledgeDocumentStatus.READY:
            return
        business = session.scalar(
            select(Business)
            .where(Business.id == document.business_id)
            .with_for_update()
        )
        if business is None or business.status is not BusinessStatus.ACTIVE:
            return
        chunks = session.scalars(
            select(KnowledgeDocumentChunk)
            .where(KnowledgeDocumentChunk.document_id == identifier)
            .order_by(KnowledgeDocumentChunk.chunk_index)
        ).all()
        if not chunks or all(
            chunk.embedding is not None
            and chunk.embedding_model == settings.embedding_model
            for chunk in chunks
        ):
            return
        texts = [chunk.content for chunk in chunks]
    try:
        vectors = embed_batched(
            create_embedding_provider(settings), texts, settings.embedding_batch_size
        )
    except EmbeddingProviderError as exc:
        if exc.retryable:
            raise
        return
    with get_session_factory()() as session:
        document = session.scalar(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.id == identifier)
            .with_for_update()
        )
        if document is None or document.status is not KnowledgeDocumentStatus.READY:
            return
        business = session.scalar(
            select(Business)
            .where(Business.id == document.business_id)
            .with_for_update()
        )
        if business is None or business.status is not BusinessStatus.ACTIVE:
            return
        chunks = session.scalars(
            select(KnowledgeDocumentChunk)
            .where(KnowledgeDocumentChunk.document_id == identifier)
            .order_by(KnowledgeDocumentChunk.chunk_index)
            .with_for_update()
        ).all()
        if len(chunks) != len(vectors):
            return
        for chunk, vector in zip(chunks, vectors, strict=True):
            chunk.embedding = vector
            chunk.embedding_model = settings.embedding_model
            chunk.embedded_at = utc_now()
        session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Queue document re-embedding.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--business-id", type=uuid.UUID)
    group.add_argument("--all", action="store_true")
    args = parser.parse_args()
    enqueue_needed(args.business_id if not args.all else None, get_settings())


if __name__ == "__main__":
    main()
