"""PostgreSQL-backed Milestone 13 retrieval and embedding lifecycle checks."""

from __future__ import annotations

import io
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.core.config import Settings
from app.database.models import (
    KnowledgeDocument,
    KnowledgeDocumentChunk,
    KnowledgeDocumentStatus,
)
from app.rag.embeddings import EmbeddingProviderError, EmbeddingResult
from app.rag.evaluation import EvaluationCase, metrics
from app.rag.reembed import enqueue_needed, process_reembed, reembed_job_id
from app.rag.retrieval import retrieve
from app.worker import knowledge
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from tests.test_business_api import change_business_status
from tests.test_knowledge_documents import document_values
from tests.test_owner_chat import active_business


def vector(value: float) -> list[float]:
    return [value] + [0.0] * 1023


class Provider:
    model = "gemini-embedding-001"

    def __init__(self, vectors: list[list[float]] | None = None) -> None:
        self.vectors = vectors or [vector(1.0)]
        self.calls = 0

    def embed(
        self, texts: list[str], *, task_type: str = "RETRIEVAL_DOCUMENT"
    ) -> EmbeddingResult:
        self.calls += 1
        return EmbeddingResult(
            vectors=tuple(
                tuple(self.vectors[index % len(self.vectors)])
                for index in range(len(texts))
            ),
            model=self.model,
        )


def ready_document(
    session: Session,
    business_id: uuid.UUID,
    *,
    digest: str,
    chunks: list[tuple[str, list[float] | None, str | None]],
    status: KnowledgeDocumentStatus = KnowledgeDocumentStatus.READY,
) -> KnowledgeDocument:
    document = KnowledgeDocument(**document_values(business_id, content_sha256=digest))
    session.add(document)
    session.flush()
    document.status = KnowledgeDocumentStatus.PROCESSING
    document.processing_started_at = datetime.now(UTC)
    session.flush()
    session.add_all(
        KnowledgeDocumentChunk(
            business_id=business_id,
            document_id=document.id,
            chunk_index=index,
            content=content,
            character_count=len(content),
            embedding=embedding,
            embedding_model=model,
            embedded_at=datetime.now(UTC) if embedding is not None else None,
        )
        for index, (content, embedding, model) in enumerate(chunks)
    )
    session.flush()
    if status is KnowledgeDocumentStatus.READY:
        document.status = status
        document.processing_completed_at = datetime.now(UTC)
    session.commit()
    return document


def test_retrieval_is_database_tenant_scoped_and_filtered(
    api_client, db_session: Session
) -> None:
    user, first = active_business(api_client, db_session, email="rag-first@example.com")
    _, second = active_business(api_client, db_session, email="rag-second@example.com")
    first_id, second_id = uuid.UUID(str(first["id"])), uuid.UUID(str(second["id"]))
    allowed = ready_document(
        db_session,
        first_id,
        digest="a" * 64,
        chunks=[
            ("first-one", vector(1.0), "gemini-embedding-001"),
            ("first-two", vector(1.0), "gemini-embedding-001"),
        ],
    )
    ready_document(
        db_session,
        first_id,
        digest="b" * 64,
        chunks=[("old-model", vector(1.0), "old-model")],
    )
    ready_document(
        db_session,
        first_id,
        digest="c" * 64,
        chunks=[("no-vector", None, None)],
    )
    ready_document(
        db_session,
        second_id,
        digest="d" * 64,
        chunks=[("foreign-secret", vector(1.0), "gemini-embedding-001")],
    )
    result = retrieve(
        db_session, user, first_id, "question", Provider(), Settings(_env_file=None)
    )
    assert result.status == "OK"
    assert [item.content for item in result.chunks] == ["first-one", "first-two"]
    assert {item.document_id for item in result.chunks} == {allowed.id}
    assert all("foreign" not in item.content for item in result.chunks)
    no_knowledge = retrieve(
        db_session,
        user,
        first_id,
        "question",
        Provider([[0.0, 1.0] + [0.0] * 1022]),
        Settings(_env_file=None),
    )
    assert no_knowledge.status == "NO_RELEVANT_KNOWLEDGE"


def test_document_worker_persists_all_embeddings_or_none(
    api_client, db_session: Session, database_engine, monkeypatch
) -> None:
    _, business = active_business(
        api_client, db_session, email="worker-rag@example.com"
    )
    identifier = uuid.UUID(str(business["id"]))
    document = KnowledgeDocument(
        **document_values(
            identifier, original_filename="source.txt", mime_type="text/plain"
        )
    )
    db_session.add(document)
    db_session.commit()
    factory = sessionmaker(bind=database_engine, expire_on_commit=False)
    statements: list[str] = []

    def record_statement(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        statements.append(" ".join(statement.lower().split()))

    event.listen(database_engine, "before_cursor_execute", record_statement)
    monkeypatch.setattr(knowledge, "get_session_factory", lambda: factory)
    monkeypatch.setattr(knowledge, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(
        knowledge,
        "get_knowledge_storage",
        lambda _: type(
            "Storage", (), {"open": lambda *_: nullcontext(io.BytesIO(b"one\n\ntwo"))}
        )(),
    )
    monkeypatch.setattr(knowledge, "create_embedding_provider", lambda _: Provider())
    try:
        knowledge.process_document(str(document.id))
    finally:
        event.remove(database_engine, "before_cursor_execute", record_statement)
    db_session.refresh(document)
    assert document.status is KnowledgeDocumentStatus.READY, document.failure_code
    assert len(document.chunks) == 1
    assert document.chunks[0].embedding_model == "gemini-embedding-001"
    assert len(document.chunks[0].embedding or []) == 1024
    chunk_insert = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("insert into knowledge_document_chunks")
    )
    document_updates = [
        index
        for index, statement in enumerate(statements)
        if statement.startswith("update knowledge_documents set ")
    ]
    assert len(document_updates) == 2
    assert chunk_insert < document_updates[-1]

    failed = KnowledgeDocument(
        **document_values(
            identifier,
            content_sha256="e" * 64,
            original_filename="failed.txt",
            mime_type="text/plain",
        )
    )
    db_session.add(failed)
    db_session.commit()
    monkeypatch.setattr(
        knowledge,
        "create_embedding_provider",
        lambda _: (_ for _ in ()).throw(
            EmbeddingProviderError("embedding_invalid_response", retryable=False)
        ),
    )
    knowledge.process_document(str(failed.id))
    db_session.refresh(failed)
    assert failed.status is KnowledgeDocumentStatus.FAILED
    assert failed.chunks == []


def test_reembedding_replaces_only_complete_outdated_sets(
    api_client, db_session: Session, database_engine, monkeypatch
) -> None:
    _, business = active_business(
        api_client, db_session, email="reembed-rag@example.com"
    )
    business_id = uuid.UUID(str(business["id"]))
    document = ready_document(
        db_session,
        business_id,
        digest="f" * 64,
        chunks=[
            ("first", vector(0.2), "old-model"),
            ("second", vector(0.3), "old-model"),
        ],
    )
    factory = sessionmaker(bind=database_engine, expire_on_commit=False)
    from app.rag import reembed

    monkeypatch.setattr(reembed, "get_session_factory", lambda: factory)
    monkeypatch.setattr(reembed, "get_settings", lambda: Settings(_env_file=None))
    provider = Provider([vector(0.8), vector(0.9)])
    monkeypatch.setattr(reembed, "create_embedding_provider", lambda _: provider)
    process_reembed(str(document.id))
    db_session.refresh(document)
    assert [chunk.embedding_model for chunk in document.chunks] == [
        "gemini-embedding-001",
        "gemini-embedding-001",
    ]
    before = [list(chunk.embedding or []) for chunk in document.chunks]
    process_reembed(str(document.id))
    assert provider.calls == 1
    assert reembed_job_id(document.id, "gemini-embedding-001") == reembed_job_id(
        document.id, "gemini-embedding-001"
    )
    for chunk in document.chunks:
        chunk.embedding_model = "old-model"
    db_session.commit()
    monkeypatch.setattr(
        reembed,
        "create_embedding_provider",
        lambda _: (_ for _ in ()).throw(
            EmbeddingProviderError("embedding_invalid_response", retryable=False)
        ),
    )
    with pytest.raises(EmbeddingProviderError, match="embedding_invalid_response"):
        process_reembed(str(document.id))
    db_session.refresh(document)
    assert [list(chunk.embedding or []) for chunk in document.chunks] == before


def test_database_evaluation_path_reports_no_cross_tenant_leakage(
    api_client, db_session: Session
) -> None:
    user, business = active_business(
        api_client, db_session, email="evaluation-rag@example.com"
    )
    _, foreign = active_business(
        api_client, db_session, email="evaluation-foreign@example.com"
    )
    business_id = uuid.UUID(str(business["id"]))
    ready_document(
        db_session,
        business_id,
        digest="1" * 64,
        chunks=[("owned", vector(0.9), "gemini-embedding-001")],
    )
    ready_document(
        db_session,
        uuid.UUID(str(foreign["id"])),
        digest="2" * 64,
        chunks=[("foreign", vector(1.0), "gemini-embedding-001")],
    )
    retrieved = retrieve(
        db_session,
        user,
        business_id,
        "fixture question",
        Provider(),
        Settings(_env_file=None),
    )
    cases = [EvaluationCase("fixture", "english", "owned", 0)]
    report = metrics(
        cases,
        {"fixture": [("owned", chunk.chunk_index) for chunk in retrieved.chunks]},
    )
    fixture = Path(__file__).parents[1] / "evaluations" / "milestone_13_retrieval.json"
    assert (
        len(__import__("json").loads(fixture.read_text(encoding="utf-8"))["cases"])
        == 30
    )
    assert report["english"]["execution_failure_rate"] == 0
    assert all(chunk.content != "foreign" for chunk in retrieved.chunks)


def test_reembedding_skips_disabled_businesses_at_selection(
    api_client, db_session: Session, database_engine, monkeypatch
) -> None:
    _, business = active_business(
        api_client, db_session, email="disabled-select@example.com"
    )
    business_id = uuid.UUID(str(business["id"]))
    document = ready_document(
        db_session,
        business_id,
        digest="3" * 64,
        chunks=[("outdated", vector(0.2), "old-model")],
    )
    change_business_status(db_session, business_id, "DISABLED")
    from app.rag import reembed

    factory = sessionmaker(bind=database_engine, expire_on_commit=False)
    queued: list[uuid.UUID] = []
    monkeypatch.setattr(reembed, "get_session_factory", lambda: factory)
    monkeypatch.setattr(
        reembed, "enqueue_reembed", lambda identifier, _: queued.append(identifier)
    )
    assert enqueue_needed(None, Settings(_env_file=None)) == 0
    assert queued == []
    assert document.id not in queued


def test_reembedding_skips_if_business_disables_before_persistence(
    api_client, db_session: Session, database_engine, monkeypatch
) -> None:
    _, business = active_business(
        api_client, db_session, email="disabled-race@example.com"
    )
    business_id = uuid.UUID(str(business["id"]))
    document = ready_document(
        db_session,
        business_id,
        digest="4" * 64,
        chunks=[("outdated", vector(0.2), "old-model")],
    )
    original = list(document.chunks[0].embedding or [])
    from app.rag import reembed

    class DisablingProvider(Provider):
        def embed(
            self, texts: list[str], *, task_type: str = "RETRIEVAL_DOCUMENT"
        ) -> EmbeddingResult:
            result = super().embed(texts)
            change_business_status(db_session, business_id, "DISABLED")
            return result

    factory = sessionmaker(bind=database_engine, expire_on_commit=False)
    monkeypatch.setattr(reembed, "get_session_factory", lambda: factory)
    monkeypatch.setattr(reembed, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(
        reembed,
        "create_embedding_provider",
        lambda _: DisablingProvider([vector(0.8)]),
    )
    process_reembed(str(document.id))
    db_session.refresh(document)
    assert document.chunks[0].embedding_model == "old-model"
    assert list(document.chunks[0].embedding or []) == original


@pytest.mark.parametrize("retryable,expected", [(True, "PENDING"), (False, "FAILED")])
def test_embedding_http_failure_respects_provider_retryability(
    api_client, db_session: Session, database_engine, monkeypatch, retryable, expected
) -> None:
    _, business = active_business(api_client, db_session)
    document = KnowledgeDocument(**document_values(uuid.UUID(business["id"])))
    db_session.add(document)
    db_session.commit()
    document.status = KnowledgeDocumentStatus.PROCESSING
    document.processing_started_at = datetime.now(UTC)
    document.processing_attempts = 1
    db_session.commit()
    monkeypatch.setattr(
        knowledge, "get_session_factory", lambda: sessionmaker(bind=database_engine)
    )
    assert (
        knowledge._fail(document.id, "embedding_http_error", retryable=retryable)
        is retryable
    )
    db_session.refresh(document)
    assert document.status == expected


def test_reembedding_does_not_write_vectors_for_replaced_same_count_chunks(
    api_client, db_session: Session, database_engine, monkeypatch
) -> None:
    _, business = active_business(api_client, db_session)
    document = ready_document(
        db_session,
        uuid.UUID(business["id"]),
        digest="f" * 64,
        chunks=[("original", vector(0.2), "old-model")],
    )
    from app.rag import reembed

    class ReplacingProvider(Provider):
        def embed(self, texts, *, task_type="RETRIEVAL_DOCUMENT"):
            document.chunks[0].content = "changed!"
            document.chunks[0].character_count = len("changed!")
            db_session.commit()
            return super().embed(texts, task_type=task_type)

    monkeypatch.setattr(
        reembed, "get_session_factory", lambda: sessionmaker(bind=database_engine)
    )
    monkeypatch.setattr(reembed, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(
        reembed, "create_embedding_provider", lambda _: ReplacingProvider()
    )
    reembed.process_reembed(str(document.id))
    db_session.refresh(document)
    assert document.chunks[0].content == "changed!"
    assert document.chunks[0].embedding_model == "old-model"


def test_owner_retry_accepts_embedding_failure_without_replacing_document(
    api_client, db_session: Session, monkeypatch
) -> None:
    user, business = active_business(api_client, db_session)
    document = KnowledgeDocument(**document_values(uuid.UUID(business["id"])))
    db_session.add(document)
    db_session.commit()
    document.status = KnowledgeDocumentStatus.PROCESSING
    document.processing_started_at = datetime.now(UTC)
    document.processing_attempts = 3
    db_session.commit()
    document.status = KnowledgeDocumentStatus.FAILED
    document.failure_code = "embedding_unavailable"
    document.processing_completed_at = datetime.now(UTC)
    db_session.commit()
    queued = []
    monkeypatch.setattr(
        knowledge,
        "enqueue_document",
        lambda identifier, settings, **kwargs: queued.append((identifier, kwargs)),
    )
    from app.services.knowledge_documents import retry

    result = retry(
        db_session, user, document.business_id, document.id, Settings(_env_file=None)
    )
    assert result.id == document.id
    assert result.status is KnowledgeDocumentStatus.PENDING
    assert queued == [(document.id, {"retry_failed": True})]


def test_ready_replacement_supersedes_old_chunks_without_deleting_history(
    api_client, db_session: Session
) -> None:
    user, business = active_business(api_client, db_session)
    business_id = uuid.UUID(business["id"])
    original = ready_document(
        db_session,
        business_id,
        digest="a" * 64,
        chunks=[("Original return policy.", vector(1), "gemini-embedding-001")],
    )
    replacement = KnowledgeDocument(
        **document_values(
            business_id, content_sha256="b" * 64, replaces_document_id=original.id
        )
    )
    db_session.add(replacement)
    db_session.commit()
    settings = Settings(_env_file=None)
    found = retrieve(
        db_session, user, business_id, "return policy", Provider(), settings
    )
    assert {chunk.document_id for chunk in found.chunks} == {original.id}
    replacement.status = KnowledgeDocumentStatus.PROCESSING
    replacement.processing_started_at = datetime.now(UTC)
    db_session.commit()
    db_session.add(
        KnowledgeDocumentChunk(
            business_id=business_id,
            document_id=replacement.id,
            chunk_index=0,
            content="Updated return policy.",
            character_count=len("Updated return policy."),
            embedding=vector(1),
            embedding_model="gemini-embedding-001",
            embedded_at=datetime.now(UTC),
        )
    )
    db_session.flush()
    replacement.status = KnowledgeDocumentStatus.READY
    replacement.processing_completed_at = datetime.now(UTC)
    db_session.commit()
    found = retrieve(
        db_session, user, business_id, "return policy", Provider(), settings
    )
    assert {chunk.document_id for chunk in found.chunks} == {replacement.id}
    assert db_session.get(KnowledgeDocument, original.id) is not None
    assert original.chunks[0].content == "Original return policy."


@pytest.mark.parametrize("chunk_count", [2, 80, 500])
def test_ingestion_small_medium_and_maximum_chunk_scope(
    api_client, db_session, database_engine, monkeypatch, chunk_count
):
    _, business = active_business(api_client, db_session)
    paragraphs = [
        f"Demonstration passage {i:04d}. " + "Policy example. " * 60
        for i in range(chunk_count)
    ]
    content = "\n\n".join(paragraph[:900] for paragraph in paragraphs)
    assert len(content) <= 500_000
    document = KnowledgeDocument(
        **document_values(
            uuid.UUID(business["id"]),
            original_filename="scope.txt",
            mime_type="text/plain",
        )
    )
    db_session.add(document)
    db_session.commit()
    monkeypatch.setattr(
        knowledge,
        "get_session_factory",
        lambda: sessionmaker(bind=database_engine, expire_on_commit=False),
    )
    monkeypatch.setattr(
        knowledge,
        "get_knowledge_storage",
        lambda _: type(
            "Storage",
            (),
            {"open": lambda *_: nullcontext(io.BytesIO(content.encode()))},
        )(),
    )
    provider = Provider()
    monkeypatch.setattr(knowledge, "create_embedding_provider", lambda _: provider)
    knowledge.process_document(str(document.id))
    db_session.refresh(document)
    assert document.status is KnowledgeDocumentStatus.READY
    assert len(document.chunks) == chunk_count
    assert {chunk.chunk_index for chunk in document.chunks} == set(range(chunk_count))
    assert all(
        chunk.embedding_model == "gemini-embedding-001" for chunk in document.chunks
    )
    assert provider.calls == (chunk_count + 15) // 16
    knowledge.process_document(str(document.id))
    assert provider.calls == (chunk_count + 15) // 16


def test_reembed_failure_is_visible_preserves_vectors_and_excludes_old_model(
    api_client, db_session, database_engine, monkeypatch
):
    from unittest.mock import Mock

    from app.rag import reembed

    user, business = active_business(api_client, db_session)
    document = ready_document(
        db_session,
        uuid.UUID(business["id"]),
        digest="c" * 64,
        chunks=[("old return policy", vector(1), "old-model")],
    )
    original = [
        (chunk.id, list(chunk.embedding), chunk.embedded_at)
        for chunk in document.chunks
    ]
    monkeypatch.setattr(
        reembed,
        "get_session_factory",
        lambda: sessionmaker(bind=database_engine, expire_on_commit=False),
    )
    job = Mock(meta={})
    monkeypatch.setattr(knowledge, "get_current_job", lambda: job)
    monkeypatch.setattr(
        reembed,
        "create_embedding_provider",
        Mock(
            side_effect=EmbeddingProviderError("embedding_dimension", retryable=False)
        ),
    )
    with pytest.raises(EmbeddingProviderError, match="embedding_dimension"):
        process_reembed(str(document.id))
    assert job.meta == {
        "embedding_failure_code": "embedding_dimension",
        "embedding_failure_retryable": False,
    }
    job.save_meta.assert_called_once()
    db_session.refresh(document)
    assert document.status is KnowledgeDocumentStatus.READY
    assert [
        (chunk.id, list(chunk.embedding), chunk.embedded_at)
        for chunk in document.chunks
    ] == original
    found = retrieve(
        db_session,
        user,
        document.business_id,
        "return policy",
        Provider(),
        Settings(_env_file=None),
    )
    assert found.chunks == ()
    # A repaired provider can retry without recreating the source or its chunks.
    monkeypatch.setattr(reembed, "create_embedding_provider", lambda _: Provider())
    process_reembed(str(document.id))
    db_session.refresh(document)
    assert [chunk.id for chunk in document.chunks] == [row[0] for row in original]
    assert document.chunks[0].embedding_model == "gemini-embedding-001"


def test_duplicate_reembed_execution_does_not_repeat_provider_calls(
    api_client, db_session, database_engine, monkeypatch
):
    from app.rag import reembed

    _, business = active_business(api_client, db_session)
    document = ready_document(
        db_session,
        uuid.UUID(business["id"]),
        digest="c" * 64,
        chunks=[("policy", vector(1), "old-model")],
    )
    monkeypatch.setattr(
        reembed,
        "get_session_factory",
        lambda: sessionmaker(bind=database_engine, expire_on_commit=False),
    )

    class ReentrantProvider(Provider):
        def embed(self, texts, **kwargs):
            process_reembed(str(document.id))
            return super().embed(texts, **kwargs)

    provider = ReentrantProvider()
    monkeypatch.setattr(reembed, "create_embedding_provider", lambda _: provider)
    process_reembed(str(document.id))
    assert provider.calls == 1


def test_reembed_does_not_overwrite_a_document_replaced_during_provider_call(
    api_client, db_session, database_engine, monkeypatch
):
    from app.rag import reembed

    _, business = active_business(api_client, db_session)
    document = ready_document(
        db_session,
        uuid.UUID(business["id"]),
        digest="c" * 64,
        chunks=[("old policy", vector(1), "old-model")],
    )
    factory = sessionmaker(bind=database_engine, expire_on_commit=False)
    monkeypatch.setattr(reembed, "get_session_factory", lambda: factory)

    class ReplacingProvider(Provider):
        def embed(self, texts, **kwargs):
            with factory() as session:
                replacement = ready_document(
                    session,
                    document.business_id,
                    digest="d" * 64,
                    chunks=[("new policy", vector(1), "gemini-embedding-001")],
                )
                replacement.replaces_document_id = document.id
                session.commit()
            return super().embed(texts, **kwargs)

    monkeypatch.setattr(
        reembed, "create_embedding_provider", lambda _: ReplacingProvider()
    )
    process_reembed(str(document.id))
    db_session.expire_all()
    assert document.chunks[0].embedding_model == "old-model"
