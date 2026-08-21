"""Owner-chat and learned-knowledge API schemas."""

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.database.models import (
    ChatMessageRole,
    KnowledgeCategory,
    KnowledgeKind,
)


def normalize_subject_key(value: str) -> str:
    """Create a provider-independent, stable lowercase fact subject."""
    normalized = "_".join(
        part for part in value.strip().casefold().replace("-", "_").split("_") if part
    )
    if not normalized or len(normalized) > 100:
        raise ValueError("Subject key must contain between 1 and 100 characters.")
    if not all(
        character.isascii() and (character.isalnum() or character == "_")
        for character in normalized
    ):
        raise ValueError(
            "Subject key may contain lowercase ASCII letters, numbers, and underscores."
        )
    return normalized


class OwnerMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str
    content: str

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        clean = value.strip()
        if not 1 <= len(clean) <= 200:
            raise ValueError(
                "Idempotency key must contain between 1 and 200 characters."
            )
        return clean

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if not 1 <= len(value.strip()) <= 4_000:
            raise ValueError("Message must contain between 1 and 4000 characters.")
        return value


class ChatMessageResponse(BaseModel):
    id: uuid.UUID
    sequence_number: int
    role: ChatMessageRole
    content: str
    created_at: datetime
    sources: list[CitationResponse] = []


class CitationResponse(BaseModel):
    label: str
    document_id: uuid.UUID | None
    filename: str
    page_start: int | None
    page_end: int | None
    section_title: str | None
    available: bool


class OwnerTurnResponse(BaseModel):
    owner_message: ChatMessageResponse
    assistant_message: ChatMessageResponse
    replayed: bool


class ConversationHistoryResponse(BaseModel):
    items: list[ChatMessageResponse]
    next_cursor: str | None


class KnowledgeResponse(BaseModel):
    id: uuid.UUID
    subject_key: str
    content: str
    kind: KnowledgeKind
    category: KnowledgeCategory
    expires_at: datetime | None
    source: str
    source_message_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class KnowledgeUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_key: str | None = None
    content: str | None = None
    kind: KnowledgeKind | None = None
    category: KnowledgeCategory | None = None
    expires_at: datetime | None = None

    @field_validator("subject_key")
    @classmethod
    def validate_subject(cls, value: str | None) -> str | None:
        return None if value is None else normalize_subject_key(value)

    @field_validator("content")
    @classmethod
    def validate_fact_content(cls, value: str | None) -> str | None:
        if value is None:
            return None
        clean = value.strip()
        if not 1 <= len(clean) <= 4000:
            raise ValueError("Knowledge must contain between 1 and 4000 characters.")
        return clean

    @field_validator("expires_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("Expiry must include a timezone.")
        return value

    @model_validator(mode="after")
    def validate_explicit_lifecycle(self) -> KnowledgeUpdateRequest:
        if self.kind is KnowledgeKind.PERMANENT and self.expires_at is not None:
            raise ValueError("Permanent knowledge cannot have an expiry.")
        if self.kind is KnowledgeKind.TEMPORARY:
            if self.expires_at is None:
                raise ValueError("Temporary knowledge requires an expiry.")
            if self.expires_at <= datetime.now(UTC):
                raise ValueError("Temporary knowledge expiry must be in the future.")
        return self
