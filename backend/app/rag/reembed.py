"""Internal queue and CLI support for safe whole-document re-embedding."""

from __future__ import annotations

import argparse
import logging
import time
import uuid

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.runtime import (
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
from app.rag.embeddings import (
    EmbeddingProviderError,
    create_embedding_provider,
    embed_batched,
)
from app.worker.knowledge import (
    clear_embedding_failure,
    enqueue_knowledge_job,
    record_embedding_failure,
)

logger = logging.getLogger(__name__)


def reembed_job_id(document_id: uuid.UUID, model: str) -> str:
    return f"knowledge-reembed-{document_id}-{model.replace(':', '-')}"


def enqueue_reembed(document_id: uuid.UUID, settings: Settings) -> None:
    enqueue_knowledge_job(
        document_id,
        settings,
        process_reembed,
        reembed_job_id(document_id, settings.embedding_model),
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
    validate_worker_job(settings, "knowledge")
    clear_embedding_failure()
    identifier = uuid.UUID(document_id)
    # A transaction-scoped advisory lock coordinates duplicate executions without
    # holding document row locks during provider requests. A dead process releases it.
    with get_session_factory()() as coordination:
        if not coordination.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"knowledge-reembed:{identifier}"},
        ):
            return
        _process_reembed(identifier, settings)


def _has_ready_replacement(session: Session, identifier: uuid.UUID) -> bool:
    return (
        session.scalar(
            select(KnowledgeDocument.id)
            .where(
                KnowledgeDocument.replaces_document_id == identifier,
                KnowledgeDocument.status == KnowledgeDocumentStatus.READY,
            )
            .limit(1)
        )
        is not None
    )


def _process_reembed(identifier: uuid.UUID, settings: Settings) -> None:
    deadline = time.monotonic() + settings.knowledge_worker_timeout_seconds - 30
    with get_session_factory()() as session:
        document = session.scalar(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.id == identifier)
            .with_for_update()
        )
        if (
            document is None
            or document.status is not KnowledgeDocumentStatus.READY
            or _has_ready_replacement(session, identifier)
        ):
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
        snapshot = [
            (chunk.id, chunk.content, chunk.embedding_model, chunk.embedded_at)
            for chunk in chunks
        ]
    try:
        vectors = embed_batched(
            create_embedding_provider(settings),
            texts,
            settings.embedding_batch_size,
            deadline=deadline,
        )
    except EmbeddingProviderError as exc:
        record_embedding_failure(exc)
        logger.error(
            "knowledge_reembed_failed code=%s retryable=%s", exc.code, exc.retryable
        )
        raise
    with get_session_factory()() as session:
        document = session.scalar(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.id == identifier)
            .with_for_update()
        )
        if (
            document is None
            or document.status is not KnowledgeDocumentStatus.READY
            or _has_ready_replacement(session, identifier)
        ):
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
        if len(chunks) != len(vectors) or snapshot != [
            (chunk.id, chunk.content, chunk.embedding_model, chunk.embedded_at)
            for chunk in chunks
        ]:
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
