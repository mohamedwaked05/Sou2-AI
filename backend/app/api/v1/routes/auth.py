"""Version 1 HTTP routes for account authentication and recovery."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_client_ip, get_current_user
from app.core.config import Settings, get_settings
from app.core.exceptions import ApplicationError
from app.database.models import User
from app.database.session import get_db_session
from app.schemas.auth import (
    AccessTokenResponse,
    CurrentUserResponse,
    EmailRequest,
    LoginRequest,
    MessageResponse,
    PasswordChangeRequest,
    PasswordResetRequest,
    RegistrationRequest,
    TokenRequest,
)
from app.services.auth import (
    CHECK_EMAIL_MESSAGE,
    FORGOT_PASSWORD_MESSAGE,
    VERIFIED_MESSAGE,
    change_password,
    forgot_password,
    login,
    logout_all,
    logout_current,
    refresh_access_token,
    register_user,
    resend_verification,
    reset_password,
    verify_email,
)
from app.services.email import EmailService, get_email_service

router = APIRouter(prefix="/auth", tags=["authentication"])

DatabaseSession = Annotated[Session, Depends(get_db_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]
TransactionalEmail = Annotated[EmailService, Depends(get_email_service)]
AuthenticatedUser = Annotated[User, Depends(get_current_user)]


def _set_refresh_cookie(
    response: Response,
    raw_token: str,
    expires_at: datetime,
    settings: Settings,
) -> None:
    max_age = max(0, int((expires_at - datetime.now(UTC)).total_seconds()))
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=raw_token,
        max_age=max_age,
        expires=expires_at,
        path=settings.refresh_cookie_path,
        domain=settings.refresh_cookie_domain,
        secure=settings.refresh_cookie_secure,
        httponly=True,
        samesite=settings.refresh_cookie_samesite,
    )


def _clear_refresh_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=settings.refresh_cookie_name,
        path=settings.refresh_cookie_path,
        domain=settings.refresh_cookie_domain,
        secure=settings.refresh_cookie_secure,
        httponly=True,
        samesite=settings.refresh_cookie_samesite,
    )


@router.post(
    "/register",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(
    body: RegistrationRequest,
    session: DatabaseSession,
    email_service: TransactionalEmail,
) -> MessageResponse:
    register_user(
        session,
        email_service,
        first_name=body.first_name,
        last_name=body.last_name,
        email=body.email,
        password=body.password,
    )
    return MessageResponse(message=CHECK_EMAIL_MESSAGE)


@router.post("/verify-email", response_model=MessageResponse)
def verify(body: TokenRequest, session: DatabaseSession) -> MessageResponse:
    verify_email(session, body.token)
    return MessageResponse(message=VERIFIED_MESSAGE)


@router.post("/resend-verification", response_model=MessageResponse)
def resend(
    body: EmailRequest,
    request: Request,
    session: DatabaseSession,
    settings: AppSettings,
    email_service: TransactionalEmail,
) -> MessageResponse:
    resend_verification(
        session,
        email_service,
        email=body.email,
        client_ip=get_client_ip(request, settings),
    )
    return MessageResponse(message=CHECK_EMAIL_MESSAGE)


@router.post("/login", response_model=AccessTokenResponse)
def sign_in(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: DatabaseSession,
    settings: AppSettings,
) -> AccessTokenResponse:
    _, access_token, refresh_token, access_expires, refresh_expires = login(
        session,
        settings,
        email=body.email,
        password=body.password,
        keep_me_signed_in=body.keep_me_signed_in,
        client_ip=get_client_ip(request, settings),
    )
    _set_refresh_cookie(response, refresh_token, refresh_expires, settings)
    return AccessTokenResponse(access_token=access_token, expires_at=access_expires)


@router.post("/refresh", response_model=AccessTokenResponse)
def refresh(
    request: Request,
    response: Response,
    session: DatabaseSession,
    settings: AppSettings,
) -> AccessTokenResponse:
    refresh_token = request.cookies.get(settings.refresh_cookie_name)
    if not refresh_token:
        raise ApplicationError(
            "Refresh session is invalid.",
            status_code=401,
            error_code="refresh_token_invalid",
        )
    access_token, replacement, access_expires, refresh_expires = refresh_access_token(
        session, settings, refresh_token
    )
    _set_refresh_cookie(response, replacement, refresh_expires, settings)
    return AccessTokenResponse(access_token=access_token, expires_at=access_expires)


@router.post("/logout", response_model=MessageResponse)
def logout(
    request: Request,
    response: Response,
    session: DatabaseSession,
    settings: AppSettings,
) -> MessageResponse:
    refresh_token = request.cookies.get(settings.refresh_cookie_name)
    logout_current(session, refresh_token)
    _clear_refresh_cookie(response, settings)
    return MessageResponse(message="Signed out successfully.")


@router.post("/logout-all", response_model=MessageResponse)
def logout_everywhere(
    response: Response,
    session: DatabaseSession,
    settings: AppSettings,
    user: AuthenticatedUser,
) -> MessageResponse:
    logout_all(session, user)
    _clear_refresh_cookie(response, settings)
    return MessageResponse(message="Signed out from all devices.")


@router.get("/me", response_model=CurrentUserResponse)
def current_user(user: AuthenticatedUser) -> User:
    return user


@router.post("/forgot-password", response_model=MessageResponse)
def request_password_reset(
    body: EmailRequest,
    request: Request,
    session: DatabaseSession,
    settings: AppSettings,
    email_service: TransactionalEmail,
) -> MessageResponse:
    forgot_password(
        session,
        email_service,
        email=body.email,
        client_ip=get_client_ip(request, settings),
    )
    return MessageResponse(message=FORGOT_PASSWORD_MESSAGE)


@router.post("/reset-password", response_model=MessageResponse)
def complete_password_reset(
    body: PasswordResetRequest, session: DatabaseSession
) -> MessageResponse:
    reset_password(session, body.token, body.password)
    return MessageResponse(message="Password reset successfully. Sign in to continue.")


@router.post("/change-password", response_model=MessageResponse)
def update_password(
    body: PasswordChangeRequest,
    request: Request,
    session: DatabaseSession,
    settings: AppSettings,
    user: AuthenticatedUser,
) -> MessageResponse:
    change_password(
        session,
        user,
        current_password=body.current_password,
        new_password=body.new_password,
        current_refresh_token=request.cookies.get(settings.refresh_cookie_name),
    )
    return MessageResponse(message="Password changed successfully.")
