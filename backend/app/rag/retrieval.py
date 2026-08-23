"""Internal tenant-scoped exact vector retrieval boundary."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.database.models import (
    BusinessStatus,
    KnowledgeDocument,
    KnowledgeDocumentChunk,
    KnowledgeDocumentStatus,
    User,
)
from app.rag.embeddings import EmbeddingProvider, EmbeddingProviderError
from app.services.businesses import load_full_access_business

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetrievedChunk:
    document_id: uuid.UUID
    document_filename: str
    chunk_id: uuid.UUID
    chunk_index: int
    page_start: int | None
    page_end: int | None
    section_title: str | None
    content: str
    similarity: float


@dataclass(frozen=True)
class RetrievalResult:
    status: str
    chunks: tuple[RetrievedChunk, ...]


def retrieve(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    question: str,
    provider: EmbeddingProvider,
    settings: Settings,
    *,
    request_id: str | None = None,
    question_embedding: Sequence[float] | None = None,
) -> RetrievalResult:
    """Embed and exactly rank only current-model chunks for an authorized tenant."""
    started = time.monotonic()
    business = load_full_access_business(session, user, business_id)
    if business.status is not BusinessStatus.ACTIVE:
        raise PermissionError("business_not_active")
    try:
        has_candidates = session.scalar(
            select(KnowledgeDocumentChunk.id)
            .join(
                KnowledgeDocument,
                KnowledgeDocument.id == KnowledgeDocumentChunk.document_id,
            )
            .where(
                KnowledgeDocumentChunk.business_id == business_id,
                KnowledgeDocument.status == KnowledgeDocumentStatus.READY,
                KnowledgeDocumentChunk.embedding.is_not(None),
                KnowledgeDocumentChunk.embedding_model == settings.embedding_model,
            )
            .limit(1)
        )
        if has_candidates is None:
            result = RetrievalResult(status="NO_RELEVANT_KNOWLEDGE", chunks=())
            _log(
                request_id,
                business_id,
                settings.embedding_model,
                result,
                started,
                "success",
            )
            return result
        vector = list(
            question_embedding
            if question_embedding is not None
            else provider.embed([question]).vectors[0]
        )
        distance = KnowledgeDocumentChunk.embedding.cosine_distance(vector)
        similarity = (1 - distance).label("similarity")
        rows = session.execute(
            select(KnowledgeDocumentChunk, KnowledgeDocument, similarity)
            .join(
                KnowledgeDocument,
                KnowledgeDocument.id == KnowledgeDocumentChunk.document_id,
            )
            .where(
                KnowledgeDocumentChunk.business_id == business_id,
                KnowledgeDocument.status == KnowledgeDocumentStatus.READY,
                KnowledgeDocumentChunk.embedding.is_not(None),
                KnowledgeDocumentChunk.embedding_model == settings.embedding_model,
            )
            .order_by(
                distance,
                KnowledgeDocumentChunk.document_id,
                KnowledgeDocumentChunk.chunk_index,
                KnowledgeDocumentChunk.id,
            )
            .limit(settings.retrieval_candidate_limit)
        ).all()
        chunks = tuple(
            RetrievedChunk(
                document_id=document.id,
                document_filename=document.original_filename,
                chunk_id=chunk.id,
                chunk_index=chunk.chunk_index,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                section_title=chunk.section_title,
                content=chunk.content,
                similarity=float(score),
            )
            for chunk, document, score in rows
            if float(score) >= settings.retrieval_minimum_similarity
        )
        result = RetrievalResult(
            status="OK" if chunks else "NO_RELEVANT_KNOWLEDGE", chunks=chunks
        )
        _log(
            request_id,
            business_id,
            settings.embedding_model,
            result,
            started,
            "success",
        )
        return result
    except EmbeddingProviderError as exc:
        _log(request_id, business_id, settings.embedding_model, None, started, exc.code)
        raise


def _log(
    request_id: str | None,
    business_id: uuid.UUID,
    model: str,
    result: RetrievalResult | None,
    started: float,
    outcome: str,
) -> None:
    chunks = result.chunks if result else ()
    logger.info(
        "retrieval id=%s business=%s model=%s results=%s high=%s ms=%s result=%s",
        request_id,
        business_id,
        model,
        len(chunks),
        max((chunk.similarity for chunk in chunks), default=None),
        round((time.monotonic() - started) * 1000),
        outcome,
    )
