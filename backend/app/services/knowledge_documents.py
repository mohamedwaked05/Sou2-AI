"""Tenant-authorized document metadata and queue lifecycle."""

from __future__ import annotations

import hashlib
import re
import tempfile
import uuid
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.exceptions import ApplicationError
from app.database.models import (
    BusinessStatus,
    KnowledgeDocument,
    KnowledgeDocumentStatus,
    User,
)
from app.rag.document_processing import DocumentProcessingError, validate_and_extract
from app.services.businesses import load_full_access_business
from app.storage.knowledge import LocalKnowledgeStorage


def _error(code: str, status_code: int = 422) -> ApplicationError:
    messages = {
        "duplicate_document": "This document is already present for this business.",
        "document_not_found": "Document was not found.",
        "business_not_active": "The business must be active.",
        "queue_unavailable": "Document processing is temporarily unavailable.",
    }
    return ApplicationError(
        messages.get(code, "The document could not be accepted."),
        status_code=status_code,
        error_code=code,
    )


def _active(session: Session, user: User, business_id: uuid.UUID):
    business = load_full_access_business(session, user, business_id, for_update=True)
    if business.status is not BusinessStatus.ACTIVE:
        raise _error("business_not_active", 403)
    return business


def _filename(name: str | None) -> str:
    value = Path(name or "").name.strip()
    value = re.sub(r"[\x00-\x1f\\/:*?\"<>|]+", "_", value)[:255]
    if not value or value in {".", ".."}:
        raise _error("invalid_document_filename")
    return value


def _read_upload(upload: UploadFile, settings: Settings) -> tuple[Path, int, str]:
    digest = hashlib.sha256()
    size = 0
    temp = tempfile.NamedTemporaryFile(delete=False)
    path = Path(temp.name)
    try:
        while block := upload.file.read(64 * 1024):
            size += len(block)
            if size > settings.knowledge_upload_max_bytes:
                raise _error("document_too_large", 413)
            digest.update(block)
            temp.write(block)
        temp.close()
        if not size:
            raise _error("empty_document")
        return path, size, digest.hexdigest()
    except Exception:
        temp.close()
        path.unlink(missing_ok=True)
        raise


def _queue(document_id: uuid.UUID, settings: Settings) -> None:
    from app.worker.knowledge import enqueue_document

    try:
        enqueue_document(document_id, settings)
    except Exception:
        raise _error("queue_unavailable", 503) from None


def upload(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    file: UploadFile,
    settings: Settings,
    replaces: uuid.UUID | None = None,
) -> KnowledgeDocument:
    _active(session, user, business_id)
    filename = _filename(file.filename)
    temporary, size, digest = _read_upload(file, settings)
    try:
        data = temporary.read_bytes()
        try:
            inspected = validate_and_extract(
                data,
                filename,
                file.content_type or "",
                settings.knowledge_max_pdf_pages,
                settings.knowledge_max_text_characters,
            )
        except DocumentProcessingError as exc:
            raise _error(exc.code) from None
        if replaces is not None:
            source = _document(session, business_id, replaces, locked=True)
            if source.status in {
                KnowledgeDocumentStatus.PENDING,
                KnowledgeDocumentStatus.PROCESSING,
            }:
                raise _error("replacement_in_progress", 409)
            if session.scalar(
                select(KnowledgeDocument.id).where(
                    KnowledgeDocument.replaces_document_id == replaces,
                    KnowledgeDocument.status.in_(
                        [
                            KnowledgeDocumentStatus.PENDING,
                            KnowledgeDocumentStatus.PROCESSING,
                        ]
                    ),
                )
            ):
                raise _error("replacement_in_progress", 409)
        document = KnowledgeDocument(
            id=uuid.uuid4(),
            business_id=business_id,
            uploaded_by_user_id=user.id,
            original_filename=filename,
            mime_type=inspected.mime_type,
            file_size_bytes=size,
            content_sha256=digest,
            storage_key="pending",
            replaces_document_id=replaces,
        )
        storage = LocalKnowledgeStorage(settings.knowledge_storage_root)
        document.storage_key = storage.store(
            business_id, document.id, temporary.open("rb")
        )
        session.add(document)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            storage.delete(business_id, document.id, document.storage_key)
            existing = session.scalar(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.business_id == business_id,
                    KnowledgeDocument.content_sha256 == digest,
                )
            )
            raise ApplicationError(
                "This document is already present for this business.",
                status_code=409,
                error_code="duplicate_document",
                details={"document_id": str(existing.id)} if existing else None,
            ) from None
        _queue(document.id, settings)
        return document
    finally:
        temporary.unlink(missing_ok=True)


def _document(
    session: Session,
    business_id: uuid.UUID,
    document_id: uuid.UUID,
    locked: bool = False,
) -> KnowledgeDocument:
    query = select(KnowledgeDocument).where(
        KnowledgeDocument.id == document_id,
        KnowledgeDocument.business_id == business_id,
    )
    if locked:
        query = query.with_for_update()
    document = session.scalar(query)
    if document is None:
        raise _error("document_not_found", 404)
    return document


def list_documents(
    session: Session, user: User, business_id: uuid.UUID
) -> list[KnowledgeDocument]:
    _active(session, user, business_id)
    return session.scalars(
        select(KnowledgeDocument)
        .where(KnowledgeDocument.business_id == business_id)
        .order_by(KnowledgeDocument.created_at, KnowledgeDocument.id)
    ).all()


def get_document(
    session: Session, user: User, business_id: uuid.UUID, document_id: uuid.UUID
) -> KnowledgeDocument:
    _active(session, user, business_id)
    return _document(session, business_id, document_id)


def retry(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    document_id: uuid.UUID,
    settings: Settings,
) -> KnowledgeDocument:
    _active(session, user, business_id)
    document = _document(session, business_id, document_id, True)
    if document.status is not KnowledgeDocumentStatus.FAILED:
        raise _error("document_not_retryable", 409)
    document.status = KnowledgeDocumentStatus.PENDING
    document.processing_started_at = None
    document.processing_completed_at = None
    document.failure_code = None
    document.failure_message = None
    document.processing_attempts = 0
    session.commit()
    _queue(document.id, settings)
    return document


def remove(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    document_id: uuid.UUID,
    settings: Settings,
) -> None:
    _active(session, user, business_id)
    document = _document(session, business_id, document_id, True)
    storage = LocalKnowledgeStorage(settings.knowledge_storage_root)
    try:
        storage.delete(business_id, document.id, document.storage_key)
    except OSError:
        raise _error("document_storage_unavailable", 503) from None
    session.delete(document)
    session.commit()
