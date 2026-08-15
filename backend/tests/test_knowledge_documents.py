"""Milestone 11 database contract tests."""

import uuid
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from app.database.models import KnowledgeDocument, KnowledgeDocumentChunk
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.test_owner_chat import active_business


def document_values(business_id: uuid.UUID, **changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "business_id": business_id,
        "original_filename": "catalog.pdf",
        "mime_type": "application/pdf",
        "file_size_bytes": 1,
        "content_sha256": "a" * 64,
        "storage_key": f"businesses/{business_id}/documents/file/original.pdf",
    }
    values.update(changes)
    return values


def test_vector_extension_and_schema(migration_engine: Engine) -> None:
    with migration_engine.connect() as connection:
        assert connection.scalar(
            text("SELECT extname = 'vector' FROM pg_extension WHERE extname = 'vector'")
        )
        assert (
            connection.scalar(
                text(
                    "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                    "WHERE attrelid = 'knowledge_document_chunks'::regclass "
                    "AND attname = 'embedding'"
                )
            )
            == "vector(1024)"
        )


def test_document_constraints_and_tenant_duplicate_rules(
    api_client, db_session: Session
) -> None:
    _, business = active_business(api_client, db_session)
    document = KnowledgeDocument(**document_values(uuid.UUID(str(business["id"]))))
    db_session.add(document)
    db_session.commit()
    db_session.add(
        KnowledgeDocument(**document_values(document.business_id, id=uuid.uuid4()))
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    for key, value in (
        ("storage_key", "https://private.example/file"),
        ("original_filename", "../file.pdf"),
        ("file_size_bytes", 0),
    ):
        db_session.add(
            KnowledgeDocument(
                **document_values(
                    document.business_id,
                    id=uuid.uuid4(),
                    content_sha256="b" * 64,
                    **{key: value},
                )
            )
        )
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()


def test_chunk_tenant_isolation_and_ready_transition(
    api_client, db_session: Session
) -> None:
    _, first = active_business(api_client, db_session)
    _, second = active_business(
        api_client,
        db_session,
        email="documents-other@example.com",
        name="Other Documents",
    )
    document = KnowledgeDocument(**document_values(uuid.UUID(str(first["id"]))))
    db_session.add(document)
    db_session.commit()
    document.status = "PROCESSING"
    document.processing_started_at = datetime.now(UTC)
    db_session.commit()
    document.status = "READY"
    document.processing_completed_at = datetime.now(UTC)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    db_session.add(
        KnowledgeDocumentChunk(
            id=uuid.uuid4(),
            business_id=uuid.UUID(str(second["id"])),
            document_id=document.id,
            chunk_index=0,
            content="text",
            character_count=4,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    db_session.add(
        KnowledgeDocumentChunk(
            id=uuid.uuid4(),
            business_id=document.business_id,
            document_id=document.id,
            chunk_index=0,
            content="text",
            character_count=4,
        )
    )
    db_session.commit()
    document.status = "READY"
    document.processing_completed_at = datetime.now(UTC)
    db_session.commit()
    assert document.chunks[0].embedding is None


def test_status_transitions_metadata_and_ready_immutability(
    api_client, db_session: Session
) -> None:
    _, business = active_business(api_client, db_session)
    document = KnowledgeDocument(**document_values(uuid.UUID(str(business["id"]))))
    db_session.add(document)
    db_session.commit()
    for status in ("READY", "FAILED"):
        document.status = status
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()
    document.status = "PROCESSING"
    document.processing_started_at = datetime.now(UTC)
    db_session.commit()
    document.status = "FAILED"
    document.processing_completed_at = datetime.now(UTC)
    document.failure_code = "processing.invalid_file"
    db_session.commit()
    document.status = "PENDING"
    document.processing_started_at = None
    document.processing_completed_at = None
    document.failure_code = None
    db_session.commit()
    document.status = "PROCESSING"
    document.processing_started_at = datetime.now(UTC)
    db_session.commit()
    chunk = KnowledgeDocumentChunk(
        id=uuid.uuid4(),
        business_id=document.business_id,
        document_id=document.id,
        chunk_index=0,
        content="valid",
        character_count=5,
    )
    db_session.add(chunk)
    db_session.commit()
    document.status = "READY"
    document.processing_completed_at = datetime.now(UTC)
    db_session.commit()
    document.status = "PENDING"
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    db_session.delete(chunk)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_replacements_and_chunk_constraints(api_client, db_session: Session) -> None:
    _, first = active_business(api_client, db_session)
    _, second = active_business(
        api_client,
        db_session,
        email="replacement-other@example.com",
        name="Replacement Other",
    )
    first_id, second_id = uuid.UUID(str(first["id"])), uuid.UUID(str(second["id"]))
    original = KnowledgeDocument(**document_values(first_id))
    foreign = KnowledgeDocument(**document_values(second_id, content_sha256="b" * 64))
    db_session.add_all([original, foreign])
    db_session.commit()
    replacement = KnowledgeDocument(
        **document_values(
            first_id, content_sha256="c" * 64, replaces_document_id=original.id
        )
    )
    db_session.add(replacement)
    db_session.commit()
    for replacement_id in (foreign.id, uuid.uuid4()):
        db_session.add(
            KnowledgeDocument(
                **document_values(
                    first_id,
                    id=replacement_id if replacement_id != foreign.id else uuid.uuid4(),
                    content_sha256=uuid.uuid4().hex * 2,
                    replaces_document_id=replacement_id,
                )
            )
        )
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()
    db_session.add(
        KnowledgeDocumentChunk(
            id=uuid.uuid4(),
            business_id=first_id,
            document_id=original.id,
            chunk_index=-1,
            content="x",
            character_count=1,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    db_session.add(
        KnowledgeDocumentChunk(
            id=uuid.uuid4(),
            business_id=first_id,
            document_id=original.id,
            chunk_index=0,
            content="x",
            character_count=2,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_migration_downgrade_reupgrade_retains_vector(
    alembic_config: Config, migration_engine: Engine
) -> None:
    command.downgrade(alembic_config, "20260813_04")
    try:
        with migration_engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_extension "
                    "WHERE extname = 'vector')"
                )
            )
            assert not connection.scalar(
                text("SELECT to_regclass('public.knowledge_documents') IS NOT NULL")
            )
            assert not connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_type "
                    "WHERE typname = 'knowledge_document_status')"
                )
            )
        command.upgrade(alembic_config, "20260815_01")
    finally:
        command.upgrade(alembic_config, "head")
