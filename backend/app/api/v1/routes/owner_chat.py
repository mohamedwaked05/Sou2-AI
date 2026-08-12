"""Authenticated owner-chat and learned-knowledge endpoints."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.agent.owner_chat_provider import OwnerChatProvider, get_owner_chat_provider
from app.api.dependencies import get_current_user
from app.core.config import Settings, get_settings
from app.database.models import User
from app.database.session import get_db_session
from app.schemas.owner_chat import (
    ConversationHistoryResponse,
    KnowledgeResponse,
    KnowledgeUpdateRequest,
    OwnerMessageRequest,
    OwnerTurnResponse,
)
from app.services.business_knowledge import (
    delete_knowledge,
    list_knowledge,
    update_knowledge,
)
from app.services.owner_chat import get_conversation_history, submit_owner_message

router = APIRouter(prefix="/businesses/{business_id}", tags=["owner-chat"])
DatabaseSession = Annotated[Session, Depends(get_db_session)]
AuthenticatedUser = Annotated[User, Depends(get_current_user)]
ChatProvider = Annotated[OwnerChatProvider, Depends(get_owner_chat_provider)]
AppSettings = Annotated[Settings, Depends(get_settings)]


@router.post("/owner-chat/messages", response_model=OwnerTurnResponse)
def submit_message(
    business_id: uuid.UUID,
    body: OwnerMessageRequest,
    session: DatabaseSession,
    user: AuthenticatedUser,
    provider: ChatProvider,
    settings: AppSettings,
) -> OwnerTurnResponse:
    return submit_owner_message(session, user, business_id, body, provider, settings)


@router.get("/owner-chat/messages", response_model=ConversationHistoryResponse)
def history(
    business_id: uuid.UUID,
    session: DatabaseSession,
    user: AuthenticatedUser,
    cursor: Annotated[str | None, Query(max_length=500)] = None,
) -> ConversationHistoryResponse:
    return get_conversation_history(session, user, business_id, cursor)


@router.get("/knowledge", response_model=list[KnowledgeResponse])
def knowledge_list(
    business_id: uuid.UUID,
    session: DatabaseSession,
    user: AuthenticatedUser,
) -> list[KnowledgeResponse]:
    return list_knowledge(session, user, business_id)


@router.patch("/knowledge/{knowledge_id}", response_model=KnowledgeResponse)
def knowledge_update(
    business_id: uuid.UUID,
    knowledge_id: uuid.UUID,
    body: KnowledgeUpdateRequest,
    session: DatabaseSession,
    user: AuthenticatedUser,
) -> KnowledgeResponse:
    return update_knowledge(session, user, business_id, knowledge_id, body)


@router.delete(
    "/knowledge/{knowledge_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def knowledge_delete(
    business_id: uuid.UUID,
    knowledge_id: uuid.UUID,
    session: DatabaseSession,
    user: AuthenticatedUser,
) -> Response:
    delete_knowledge(session, user, business_id, knowledge_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
