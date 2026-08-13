"""Authenticated owner-facing AI usage summary endpoint."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, run_security_record_cleanup
from app.database.models import User
from app.database.session import get_db_session
from app.schemas.ai_usage import CurrentAIUsageResponse
from app.services.ai_usage import get_current_ai_usage

router = APIRouter(prefix="/businesses/{business_id}/ai-usage", tags=["ai-usage"])
DatabaseSession = Annotated[Session, Depends(get_db_session)]
AuthenticatedUser = Annotated[User, Depends(get_current_user)]


@router.get(
    "/current",
    response_model=CurrentAIUsageResponse,
    dependencies=[Depends(run_security_record_cleanup)],
)
def current_usage(
    business_id: uuid.UUID,
    session: DatabaseSession,
    user: AuthenticatedUser,
) -> CurrentAIUsageResponse:
    return get_current_ai_usage(session, user, business_id)
