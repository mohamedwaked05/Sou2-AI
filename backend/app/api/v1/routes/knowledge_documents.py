import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Response, UploadFile, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.core.config import Settings, get_settings
from app.database.models import User
from app.database.session import get_db_session
from app.schemas.knowledge_documents import KnowledgeDocumentResponse
from app.services.knowledge_documents import (
    get_document,
    list_documents,
    remove,
    retry,
    upload,
)

router = APIRouter(
    prefix="/businesses/{business_id}/knowledge/documents", tags=["knowledge-documents"]
)
DatabaseSession = Annotated[Session, Depends(get_db_session)]
AuthenticatedUser = Annotated[User, Depends(get_current_user)]
AppSettings = Annotated[Settings, Depends(get_settings)]


@router.post(
    "", response_model=KnowledgeDocumentResponse, status_code=status.HTTP_202_ACCEPTED
)
def create(
    business_id: uuid.UUID,
    file: Annotated[UploadFile, File(...)],
    session: DatabaseSession,
    user: AuthenticatedUser,
    settings: AppSettings,
) -> KnowledgeDocumentResponse:
    return upload(session, user, business_id, file, settings)


@router.get("", response_model=list[KnowledgeDocumentResponse])
def list_all(
    business_id: uuid.UUID, session: DatabaseSession, user: AuthenticatedUser
) -> list[KnowledgeDocumentResponse]:
    return list_documents(session, user, business_id)


@router.get("/{document_id}", response_model=KnowledgeDocumentResponse)
def detail(
    business_id: uuid.UUID,
    document_id: uuid.UUID,
    session: DatabaseSession,
    user: AuthenticatedUser,
) -> KnowledgeDocumentResponse:
    return get_document(session, user, business_id, document_id)


@router.post(
    "/{document_id}/replacement",
    response_model=KnowledgeDocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def replacement(
    business_id: uuid.UUID,
    document_id: uuid.UUID,
    file: Annotated[UploadFile, File(...)],
    session: DatabaseSession,
    user: AuthenticatedUser,
    settings: AppSettings,
) -> KnowledgeDocumentResponse:
    return upload(session, user, business_id, file, settings, document_id)


@router.post(
    "/{document_id}/retry",
    response_model=KnowledgeDocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_document(
    business_id: uuid.UUID,
    document_id: uuid.UUID,
    session: DatabaseSession,
    user: AuthenticatedUser,
    settings: AppSettings,
) -> KnowledgeDocumentResponse:
    return retry(session, user, business_id, document_id, settings)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    business_id: uuid.UUID,
    document_id: uuid.UUID,
    session: DatabaseSession,
    user: AuthenticatedUser,
    settings: AppSettings,
) -> Response:
    remove(session, user, business_id, document_id, settings)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
