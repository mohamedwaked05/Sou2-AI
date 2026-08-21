import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.database.models import KnowledgeDocumentStatus


class KnowledgeDocumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    original_filename: str
    mime_type: str
    file_size_bytes: int
    content_sha256: str
    status: KnowledgeDocumentStatus
    failure_code: str | None
    page_count: int | None
    replaces_document_id: uuid.UUID | None
    processing_started_at: datetime | None
    processing_completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
