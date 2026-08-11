"""Authenticated version 1 business-management endpoints."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.database.models import User
from app.database.session import get_db_session
from app.schemas.business import (
    BusinessCreateRequest,
    BusinessResponse,
    BusinessUpdateRequest,
)
from app.services.businesses import (
    confirm_onboarding,
    create_business,
    get_business,
    list_businesses,
    update_business,
)

router = APIRouter(prefix="/businesses", tags=["businesses"])
DatabaseSession = Annotated[Session, Depends(get_db_session)]
AuthenticatedUser = Annotated[User, Depends(get_current_user)]


@router.post("", response_model=BusinessResponse, status_code=status.HTTP_201_CREATED)
def create(
    body: BusinessCreateRequest, session: DatabaseSession, user: AuthenticatedUser
) -> BusinessResponse:
    return create_business(session, user, body.name)


@router.get("", response_model=list[BusinessResponse])
def list_owned(
    session: DatabaseSession, user: AuthenticatedUser
) -> list[BusinessResponse]:
    return list_businesses(session, user)


@router.get("/{business_id}", response_model=BusinessResponse)
def detail(
    business_id: uuid.UUID, session: DatabaseSession, user: AuthenticatedUser
) -> BusinessResponse:
    return get_business(session, user, business_id)


@router.patch("/{business_id}", response_model=BusinessResponse)
def update(
    business_id: uuid.UUID,
    body: BusinessUpdateRequest,
    session: DatabaseSession,
    user: AuthenticatedUser,
) -> BusinessResponse:
    return update_business(session, user, business_id, body)


@router.post("/{business_id}/onboarding/confirm", response_model=BusinessResponse)
def confirm(
    business_id: uuid.UUID, session: DatabaseSession, user: AuthenticatedUser
) -> BusinessResponse:
    return confirm_onboarding(session, user, business_id)
