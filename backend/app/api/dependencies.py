"""Shared HTTP dependencies for authenticated API endpoints."""

import ipaddress
from typing import Annotated

from fastapi import Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.exceptions import ApplicationError
from app.core.security import decode_access_token
from app.database.models import AccountStatus, User
from app.database.session import get_db_session
from app.services.auth_event_retention import cleanup_authentication_events_best_effort

bearer_scheme = HTTPBearer(auto_error=False)


def run_authentication_event_cleanup(
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Opportunistically run isolated, best-effort authentication maintenance."""
    cleanup_authentication_events_best_effort(settings)


def get_client_ip(request: Request, settings: Settings) -> str:
    """Use forwarded addresses only when the deployment explicitly trusts its proxy."""
    candidate = request.client.host if request.client is not None else "0.0.0.0"
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            candidate = forwarded.split(",", maxsplit=1)[0].strip()
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return "0.0.0.0"


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[Session, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> User:
    unauthorized = ApplicationError(
        "Authentication is required.",
        status_code=status.HTTP_401_UNAUTHORIZED,
        error_code="authentication_required",
    )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized
    try:
        user_id = decode_access_token(credentials.credentials, settings)
    except ValueError:
        raise unauthorized from None
    user = session.get(User, user_id)
    if user is None:
        raise unauthorized
    if user.status is AccountStatus.DISABLED:
        raise ApplicationError(
            "This account is disabled.",
            status_code=status.HTTP_403_FORBIDDEN,
            error_code="account_disabled",
        )
    return user
