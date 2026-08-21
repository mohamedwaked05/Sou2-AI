"""Focused unit coverage for queue identity and failure classification."""

import uuid

from app.services.knowledge_documents import RETRYABLE_FAILURE_CODES
from app.worker.knowledge import PERMANENT, document_job_id


def test_document_job_identity_is_deterministic_and_non_secret() -> None:
    identifier = uuid.uuid4()
    assert document_job_id(identifier) == document_job_id(identifier)
    assert document_job_id(identifier).endswith(str(identifier))


def test_retryable_and_permanent_codes_do_not_overlap() -> None:
    assert RETRYABLE_FAILURE_CODES.isdisjoint(PERMANENT)
    assert "processing_unavailable" in RETRYABLE_FAILURE_CODES
    assert "malformed_document" in PERMANENT
