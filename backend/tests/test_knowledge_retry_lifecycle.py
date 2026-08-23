"""Focused unit coverage for queue identity and failure classification."""

import io
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, Mock

import pytest
from app.core.config import Settings
from app.database.models import KnowledgeDocument, KnowledgeDocumentStatus
from app.services.knowledge_documents import RETRYABLE_FAILURE_CODES
from app.worker.knowledge import PERMANENT, document_job_id, enqueue_document
from sqlalchemy.orm import Session, sessionmaker


def test_document_job_identity_is_deterministic_and_non_secret() -> None:
    identifier = uuid.uuid4()
    assert document_job_id(identifier) == document_job_id(identifier)
    assert document_job_id(identifier).endswith(str(identifier))


def test_retryable_and_permanent_codes_do_not_overlap() -> None:
    assert RETRYABLE_FAILURE_CODES.isdisjoint(PERMANENT)
    assert "processing_unavailable" in RETRYABLE_FAILURE_CODES
    assert "malformed_document" in PERMANENT


def test_terminal_job_is_replaced_but_active_job_is_not(monkeypatch) -> None:

    from app.worker import knowledge
    from rq.job import JobStatus

    settings = Mock(
        knowledge_queue_name="test",
        redis_url="redis://unused",
        knowledge_worker_timeout_seconds=120,
    )
    queue = Mock()
    terminal = Mock()
    terminal.get_status.return_value = JobStatus.FAILED
    queue.fetch_job.return_value = terminal
    monkeypatch.setattr(knowledge, "Queue", lambda *_args, **_kwargs: queue)
    monkeypatch.setattr(knowledge.Redis, "from_url", lambda _url: object())
    enqueue_document(uuid.uuid4(), settings)
    terminal.delete.assert_called_once()
    queue.enqueue.assert_called_once()
    active = Mock()
    active.get_status.return_value = JobStatus.STARTED
    queue.fetch_job.return_value = active
    enqueue_document(uuid.uuid4(), settings)
    assert queue.enqueue.call_count == 1


def test_stale_processing_is_reclaimed_before_a_real_attempt(
    api_client, db_session: Session, database_engine, monkeypatch
) -> None:
    from app.worker import knowledge

    from tests.test_owner_chat import active_business

    _, business = active_business(api_client, db_session)
    document = KnowledgeDocument(
        business_id=uuid.UUID(business["id"]),
        uploaded_by_user_id=None,
        original_filename="source.txt",
        mime_type="text/plain",
        file_size_bytes=1,
        content_sha256="a" * 64,
        storage_key=f"businesses/{business['id']}/knowledge/{uuid.uuid4()}/source",
        status=KnowledgeDocumentStatus.PROCESSING,
        processing_attempts=1,
        processing_started_at=datetime.now(UTC) - timedelta(seconds=121),
    )
    db_session.add(document)
    db_session.commit()
    factory = sessionmaker(bind=database_engine, expire_on_commit=False)
    monkeypatch.setattr(knowledge, "get_session_factory", lambda: factory)
    monkeypatch.setattr(
        knowledge, "get_knowledge_storage", lambda _: MagicMock(open=MagicMock())
    )
    monkeypatch.setattr(
        knowledge,
        "validate_and_extract",
        Mock(side_effect=knowledge.DocumentProcessingError("malformed_document")),
    )
    knowledge.process_document(str(document.id))
    db_session.refresh(document)
    assert document.status is KnowledgeDocumentStatus.FAILED
    assert document.processing_attempts == 2


def test_unexpected_worker_failure_reaches_failed_after_retry_limit(
    api_client, db_session: Session, database_engine, monkeypatch
) -> None:
    from app.worker import knowledge

    from tests.test_owner_chat import active_business

    _, business = active_business(
        api_client, db_session, email="worker-failure@example.com"
    )
    document = KnowledgeDocument(
        business_id=uuid.UUID(business["id"]),
        uploaded_by_user_id=None,
        original_filename="source.txt",
        mime_type="text/plain",
        file_size_bytes=1,
        content_sha256="b" * 64,
        storage_key=f"businesses/{business['id']}/knowledge/{uuid.uuid4()}/source",
    )
    db_session.add(document)
    db_session.commit()
    factory = sessionmaker(bind=database_engine, expire_on_commit=False)
    monkeypatch.setattr(knowledge, "get_session_factory", lambda: factory)
    monkeypatch.setattr(knowledge, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(
        knowledge,
        "get_knowledge_storage",
        lambda _: type(
            "Storage",
            (),
            {"open": lambda *_: nullcontext(io.BytesIO(b"document content"))},
        )(),
    )
    monkeypatch.setattr(
        knowledge,
        "validate_and_extract",
        Mock(side_effect=RuntimeError("unexpected processor failure")),
    )

    for _ in range(2):
        with pytest.raises(RuntimeError, match="unexpected processor failure"):
            knowledge.process_document(str(document.id))
    knowledge.process_document(str(document.id))

    db_session.refresh(document)
    assert document.status is KnowledgeDocumentStatus.FAILED
    assert document.processing_attempts == 3
    assert document.failure_code == "processing_unavailable"
    assert document.processing_completed_at is not None
