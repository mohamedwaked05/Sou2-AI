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
from rq.job import JobStatus
from sqlalchemy.orm import Session, sessionmaker


def test_document_job_identity_is_deterministic_and_non_secret() -> None:
    identifier = uuid.uuid4()
    assert document_job_id(identifier) == document_job_id(identifier)
    assert document_job_id(identifier).endswith(str(identifier))


def test_explicit_owner_retry_can_recover_repaired_provider_configuration() -> None:
    assert RETRYABLE_FAILURE_CODES & PERMANENT == {
        "embedding_model_missing",
        "embedding_http_error",
    }
    assert "processing_unavailable" in RETRYABLE_FAILURE_CODES
    assert "malformed_document" in PERMANENT


def test_terminal_job_is_retained_and_active_job_is_not_requeued(monkeypatch) -> None:

    from app.worker import knowledge
    from rq.job import JobStatus

    settings = Settings(
        _env_file=None, knowledge_queue_name="test", redis_url="redis://unused"
    )
    queue = Mock()
    terminal = Mock()
    terminal.get_status.return_value = JobStatus.FAILED
    from app.core.runtime import job_contract

    terminal.meta = {"runtime_contract": job_contract(settings, "knowledge")}
    queue.fetch_job.side_effect = [terminal, None]
    monkeypatch.setattr(knowledge, "Queue", lambda *_args, **_kwargs: queue)
    monkeypatch.setattr(knowledge.Redis, "from_url", lambda _url: object())
    enqueue_document(uuid.uuid4(), settings)
    terminal.delete.assert_not_called()
    queue.enqueue.assert_called_once()
    active = Mock()
    active.get_status.return_value = JobStatus.STARTED
    queue.fetch_job.side_effect = None
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
        processing_started_at=datetime.now(UTC)
        - timedelta(
            seconds=Settings(_env_file=None).knowledge_worker_timeout_seconds + 1
        ),
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


@pytest.mark.parametrize("previous_status", [JobStatus.FAILED, JobStatus.FINISHED])
def test_explicit_document_retry_retains_previous_job(monkeypatch, previous_status):
    from app.core import runtime
    from app.worker import knowledge

    configured = Settings(_env_file=None)
    previous = Mock()
    previous.get_status.return_value = previous_status
    previous.meta = {"runtime_contract": {"source_version": "previous-release"}}
    queue = Mock()
    queue.fetch_job.side_effect = [previous, None]
    monkeypatch.setattr(knowledge, "Queue", lambda *args, **kwargs: queue)
    monkeypatch.setattr(knowledge.Redis, "from_url", lambda *args: object())
    identifier = uuid.uuid4()
    knowledge.enqueue_document(identifier, configured, retry_failed=True)
    previous.delete.assert_not_called()
    assert queue.enqueue.call_args.kwargs["job_id"].startswith(
        knowledge.document_job_id(identifier) + "-retry-"
    )
    assert queue.enqueue.call_args.kwargs["meta"][
        "runtime_contract"
    ] == runtime.job_contract(configured, "knowledge")


def test_reembed_retains_terminal_job_evidence(monkeypatch):
    from app.rag import reembed

    configured = Settings(_env_file=None)
    previous = Mock()
    previous.get_status.return_value = JobStatus.FINISHED
    queue = Mock()
    queue.fetch_job.side_effect = [previous, None]
    from app.worker import knowledge

    monkeypatch.setattr(knowledge, "Queue", lambda *args, **kwargs: queue)
    monkeypatch.setattr(knowledge.Redis, "from_url", lambda *args: object())
    identifier = uuid.uuid4()
    reembed.enqueue_reembed(identifier, configured)
    previous.delete.assert_not_called()
    assert queue.enqueue.call_args.kwargs["job_id"].startswith(
        reembed.reembed_job_id(identifier, configured.embedding_model) + "-retry-"
    )


def test_duplicate_retry_scheduling_uses_one_unique_successor(monkeypatch):
    from app.core.runtime import job_contract
    from app.worker import knowledge

    configured = Settings(_env_file=None)
    identifier = uuid.uuid4()
    base = knowledge.document_job_id(identifier)
    terminal = Mock(meta={"runtime_contract": job_contract(configured, "knowledge")})
    terminal.get_status.return_value = JobStatus.FAILED
    jobs = {base: terminal}
    queue = Mock()
    queue.fetch_job.side_effect = jobs.get

    def enqueue(*args, **kwargs):
        assert kwargs["unique"] is True
        active = Mock()
        active.get_status.return_value = JobStatus.QUEUED
        jobs[kwargs["job_id"]] = active

    queue.enqueue.side_effect = enqueue
    monkeypatch.setattr(knowledge, "Queue", lambda *args, **kwargs: queue)
    monkeypatch.setattr(knowledge.Redis, "from_url", lambda *args: object())
    knowledge.enqueue_document(identifier, configured)
    knowledge.enqueue_document(identifier, configured)
    queue.enqueue.assert_called_once()
    terminal.delete.assert_not_called()


def test_interrupted_processing_is_recovered_without_touching_recent_attempts(
    api_client, db_session, database_engine, monkeypatch
):
    from app.worker import knowledge

    from tests.test_knowledge_documents import document_values
    from tests.test_owner_chat import active_business

    _, business = active_business(api_client, db_session)
    configured = Settings(_env_file=None)
    stale = KnowledgeDocument(
        **document_values(
            uuid.UUID(business["id"]),
            status=KnowledgeDocumentStatus.PROCESSING,
            processing_attempts=1,
            processing_started_at=datetime.now(UTC)
            - timedelta(seconds=configured.knowledge_worker_timeout_seconds + 31),
        )
    )
    recent = KnowledgeDocument(
        **document_values(
            uuid.UUID(business["id"]),
            content_sha256="b" * 64,
            status=KnowledgeDocumentStatus.PROCESSING,
            processing_attempts=1,
            processing_started_at=datetime.now(UTC),
        )
    )
    db_session.add_all([stale, recent])
    db_session.commit()
    monkeypatch.setattr(
        knowledge,
        "get_session_factory",
        lambda: sessionmaker(bind=database_engine, expire_on_commit=False),
    )
    assert knowledge.recover_stale_documents(configured) == 1
    assert knowledge.recover_stale_documents(configured) == 0
    db_session.refresh(stale)
    db_session.refresh(recent)
    assert stale.status is KnowledgeDocumentStatus.FAILED
    assert stale.failure_code == "processing_timeout"
    assert stale.processing_attempts == 1
    assert recent.status is KnowledgeDocumentStatus.PROCESSING
    old_claim = (stale.processing_started_at, stale.processing_attempts)
    # A late result from the failed attempt cannot fail the newer attempt.
    stale.status = KnowledgeDocumentStatus.PENDING
    stale.failure_code = None
    stale.processing_started_at = None
    stale.processing_completed_at = None
    db_session.commit()
    stale.status = KnowledgeDocumentStatus.PROCESSING
    stale.processing_started_at = datetime.now(UTC)
    stale.processing_attempts = 2
    db_session.commit()
    assert not knowledge._fail(stale.id, "processing_unavailable", claim=old_claim)
    db_session.refresh(stale)
    assert stale.status is KnowledgeDocumentStatus.PROCESSING
    assert stale.failure_code is None
    job = Mock(
        args=[str(stale.id)],
        meta={
            "document_attempt_started_at": stale.processing_started_at.isoformat(),
            "document_attempt": 2,
        },
        retries_left=0,
    )
    knowledge.document_job_failed(job, None, None, None, None)
    db_session.refresh(stale)
    assert stale.status is KnowledgeDocumentStatus.FAILED
    assert stale.failure_code == "processing_timeout"


def test_permanent_reembed_failure_does_not_repeat_and_retains_safe_code(monkeypatch):
    from app.worker.runtime import PrivacySafeWorker
    from rq import Worker

    job = Mock(
        meta={
            "embedding_failure_code": "embedding_dimension",
            "embedding_failure_retryable": False,
        },
        retries_left=2,
    )
    captured = {}
    monkeypatch.setattr(
        Worker,
        "handle_job_failure",
        lambda self, job, queue, **kwargs: captured.update(kwargs),
    )
    worker = object.__new__(PrivacySafeWorker)
    worker.handle_job_failure(job, None, exc_string="private provider response")
    assert job.retries_left == 0
    assert "embedding_dimension" in captured["exc_string"]
    assert "private provider response" not in captured["exc_string"]


def test_worker_schedules_the_provider_delay_without_consuming_execution_time(
    monkeypatch,
):
    from app.worker.runtime import PrivacySafeWorker
    from rq import Worker

    job = Mock(
        meta={
            "embedding_failure_code": "embedding_rate_limited",
            "embedding_failure_retryable": True,
            "embedding_http_status": 429,
            "embedding_retry_after_seconds": 15647,
        },
        retries_left=2,
    )
    captured = {}
    monkeypatch.setattr(
        Worker,
        "handle_job_failure",
        lambda self, job, queue, **kwargs: captured.update(kwargs),
    )
    worker = object.__new__(PrivacySafeWorker)
    worker.handle_job_failure(job, None)
    assert job.retries_left == 2
    assert job.retry_intervals == [15647]
    assert "retry_after_seconds=15647" in captured["exc_string"]


def test_next_attempt_does_not_inherit_a_previous_quota_delay(monkeypatch):
    from app.worker.runtime import PrivacySafeWorker
    from rq import Worker
    from rq.job import Job

    job = Job.create(
        "app.worker.knowledge.process_document",
        args=[str(uuid.uuid4())],
        connection=Mock(),
    )
    job.meta = {"embedding_retry_after_seconds": 15647}
    job.retry_intervals = [15647]
    # Redis contains the current attempt's metadata, cleared before interruption.
    monkeypatch.setattr(Job, "get_meta", lambda self, refresh=True: {})
    monkeypatch.setattr(Worker, "handle_job_failure", Mock())
    worker = object.__new__(PrivacySafeWorker)
    worker.handle_job_failure(job, None)
    assert job.retry_intervals == [2, 8]
