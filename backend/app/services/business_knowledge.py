"""Tenant-scoped learned business knowledge operations."""

import uuid
from datetime import UTC, datetime

from fastapi import status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent.owner_chat_provider import ProposedKnowledge
from app.core.exceptions import ApplicationError
from app.database.models import (
    BusinessKnowledge,
    KnowledgeCategory,
    KnowledgeKind,
    User,
)
from app.schemas.owner_chat import (
    KnowledgeResponse,
    KnowledgeUpdateRequest,
    normalize_subject_key,
)
from app.services.businesses import load_full_access_business


def _response(record: BusinessKnowledge) -> KnowledgeResponse:
    return KnowledgeResponse.model_validate(record, from_attributes=True)


def _knowledge_not_found() -> ApplicationError:
    return ApplicationError(
        "Knowledge record was not found.",
        status_code=status.HTTP_404_NOT_FOUND,
        error_code="knowledge_not_found",
    )


def accepted_proposed_knowledge(
    proposed: ProposedKnowledge, now: datetime
) -> tuple[str, str, KnowledgeKind, KnowledgeCategory, datetime | None] | None:
    """Validate model-proposed facts using application-owned allowlists."""
    try:
        subject_key = normalize_subject_key(proposed.subject_key)
        kind = KnowledgeKind(proposed.kind)
        category = KnowledgeCategory(proposed.category)
    except TypeError, ValueError:
        return None
    content = proposed.content.strip() if isinstance(proposed.content, str) else ""
    if not 1 <= len(content) <= 4000:
        return None
    expiry = proposed.expires_at
    if kind == KnowledgeKind.PERMANENT:
        if expiry is not None:
            return None
    elif (
        expiry is None
        or expiry.utcoffset() is None
        or expiry.astimezone(UTC) <= now.astimezone(UTC)
    ):
        return None
    return subject_key, content, kind, category, expiry


def upsert_proposed_knowledge(
    session: Session,
    business_id: uuid.UUID,
    source_message_id: uuid.UUID,
    proposed_items: tuple[ProposedKnowledge, ...],
    now: datetime,
) -> None:
    """Race-safely create or update validated same-subject facts."""
    for proposed in proposed_items:
        accepted = accepted_proposed_knowledge(proposed, now)
        if accepted is None:
            continue
        subject_key, content, kind, category, expiry = accepted
        statement = insert(BusinessKnowledge).values(
            id=uuid.uuid4(),
            business_id=business_id,
            subject_key=subject_key,
            content=content,
            kind=kind,
            category=category,
            expires_at=expiry,
            source="owner_chat",
            source_message_id=source_message_id,
            created_at=now,
            updated_at=now,
        )
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    BusinessKnowledge.business_id,
                    BusinessKnowledge.subject_key,
                ],
                set_={
                    "content": statement.excluded.content,
                    "kind": statement.excluded.kind,
                    "category": statement.excluded.category,
                    "expires_at": statement.excluded.expires_at,
                    "source": "owner_chat",
                    "source_message_id": statement.excluded.source_message_id,
                    "updated_at": now,
                },
            )
        )


def list_knowledge(
    session: Session, user: User, business_id: uuid.UUID
) -> list[KnowledgeResponse]:
    load_full_access_business(session, user, business_id)
    records = session.scalars(
        select(BusinessKnowledge)
        .where(BusinessKnowledge.business_id == business_id)
        .order_by(BusinessKnowledge.created_at, BusinessKnowledge.id)
    ).all()
    return [_response(record) for record in records]


def _load_record(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    knowledge_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> BusinessKnowledge:
    load_full_access_business(session, user, business_id)
    query = select(BusinessKnowledge).where(
        BusinessKnowledge.id == knowledge_id,
        BusinessKnowledge.business_id == business_id,
    )
    if for_update:
        query = query.with_for_update()
    record = session.scalar(query)
    if record is None:
        raise _knowledge_not_found()
    return record


def update_knowledge(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    knowledge_id: uuid.UUID,
    body: KnowledgeUpdateRequest,
) -> KnowledgeResponse:
    record = _load_record(session, user, business_id, knowledge_id, for_update=True)
    changes = body.model_dump(exclude_unset=True)
    for required in ("subject_key", "content", "kind", "category"):
        if required in changes and changes[required] is None:
            session.rollback()
            raise ApplicationError(
                f"{required} cannot be cleared.",
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                error_code="invalid_knowledge",
            )
    for field, value in changes.items():
        setattr(record, field, value)
    if "kind" in changes and record.kind == KnowledgeKind.PERMANENT:
        record.expires_at = None
    now = datetime.now(UTC)
    lifecycle_changed = "kind" in changes or "expires_at" in changes
    if record.kind == KnowledgeKind.PERMANENT and record.expires_at is not None:
        session.rollback()
        raise ApplicationError(
            "Permanent knowledge cannot have an expiry.",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            error_code="invalid_knowledge_expiry",
        )
    if (
        lifecycle_changed
        and record.kind == KnowledgeKind.TEMPORARY
        and (record.expires_at is None or record.expires_at <= now)
    ):
        session.rollback()
        raise ApplicationError(
            "Temporary knowledge requires a future expiry.",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            error_code="invalid_knowledge_expiry",
        )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ApplicationError(
            "Knowledge with this subject already exists.",
            status_code=status.HTTP_409_CONFLICT,
            error_code="knowledge_subject_conflict",
        ) from None
    return _response(record)


def delete_knowledge(
    session: Session,
    user: User,
    business_id: uuid.UUID,
    knowledge_id: uuid.UUID,
) -> None:
    record = _load_record(session, user, business_id, knowledge_id, for_update=True)
    session.delete(record)
    session.commit()
