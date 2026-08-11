"""Authentication application logic and transactional session management."""

import uuid
from datetime import timedelta

from fastapi import status
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.exceptions import ApplicationError
from app.core.security import (
    create_access_token,
    generate_opaque_token,
    hash_password,
    normalize_email,
    password_policy_violations,
    token_digest,
    token_digest_matches,
    utc_now,
    verify_password,
)
from app.database.models import (
    AccountStatus,
    AuthenticationEvent,
    EmailVerificationToken,
    PasswordResetToken,
    RefreshSession,
    User,
)
from app.services.email import EmailDeliveryError, EmailService

CHECK_EMAIL_MESSAGE = "Check your email to verify your account."
VERIFIED_MESSAGE = "Email verified successfully."
FORGOT_PASSWORD_MESSAGE = (
    "If an account exists for this email, a password-reset link has been sent."
)
INVALID_CREDENTIALS_MESSAGE = "Invalid email or password."
_DUMMY_PASSWORD_HASH = hash_password("Timing-only-password-1!")


def _error(
    detail: str,
    status_code: int,
    error_code: str,
    *,
    details: dict[str, object] | None = None,
) -> ApplicationError:
    return ApplicationError(
        detail, status_code=status_code, error_code=error_code, details=details
    )


def _enforce_password_policy(password: str, user: User) -> None:
    violations = password_policy_violations(
        password,
        first_name=user.first_name,
        last_name=user.last_name,
        email=user.email,
    )
    if violations:
        raise _error(
            "Password does not meet the required policy: " + ", ".join(violations),
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "password_policy_violation",
            details={"violations": violations},
        )


def _new_verification_token(user: User) -> tuple[EmailVerificationToken, str]:
    raw_token = generate_opaque_token()
    return (
        EmailVerificationToken(
            user=user,
            token_hash=token_digest(raw_token),
            expires_at=utc_now() + timedelta(hours=24),
        ),
        raw_token,
    )


def register_user(
    session: Session,
    email_service: EmailService,
    *,
    first_name: str,
    last_name: str,
    email: str,
    password: str,
) -> User:
    normalized_email = normalize_email(email)
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:email, 0))"),
        {"email": normalized_email},
    )
    if session.scalar(select(User).where(func.lower(User.email) == normalized_email)):
        raise _error(
            "An account with this email already exists.",
            status.HTTP_409_CONFLICT,
            "email_already_registered",
        )

    user = User(
        first_name=first_name,
        last_name=last_name,
        email=normalized_email,
        password_hash="pending",
    )
    _enforce_password_policy(password, user)
    user.password_hash = hash_password(password)
    verification, raw_token = _new_verification_token(user)
    session.add_all([user, verification])
    session.flush()
    try:
        email_service.send_verification_email(user.email, raw_token)
    except EmailDeliveryError:
        session.rollback()
        raise _error(
            "Verification email could not be sent. Please try again.",
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "email_delivery_unavailable",
        ) from None
    session.commit()
    return user


def verify_email(session: Session, raw_token: str) -> User:
    now = utc_now()
    token = session.scalar(
        select(EmailVerificationToken)
        .where(EmailVerificationToken.token_hash == token_digest(raw_token))
        .with_for_update()
    )
    if token is None or not token_digest_matches(raw_token, token.token_hash):
        raise _error(
            "Verification token is invalid.", 400, "verification_token_invalid"
        )
    if token.consumed_at is not None or token.invalidated_at is not None:
        raise _error(
            "Verification token is no longer valid.", 400, "verification_token_used"
        )
    if token.expires_at <= now:
        raise _error(
            "Verification token has expired.", 400, "verification_token_expired"
        )
    user = session.get(User, token.user_id)
    if user is None:
        raise _error(
            "Verification token is invalid.", 400, "verification_token_invalid"
        )
    if user.email_verified_at is not None:
        raise _error("Email is already verified.", 400, "email_already_verified")
    token.consumed_at = now
    user.email_verified_at = now
    session.commit()
    return user


def _event_count(
    session: Session,
    event_type: str,
    email: str,
    client_ip: str,
    since: object,
) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(AuthenticationEvent)
            .where(
                AuthenticationEvent.event_type == event_type,
                AuthenticationEvent.normalized_email == email,
                AuthenticationEvent.client_ip == client_ip,
                AuthenticationEvent.created_at >= since,
            )
        )
        or 0
    )


def _record_event(
    session: Session, event_type: str, email: str, client_ip: str
) -> None:
    session.add(
        AuthenticationEvent(
            event_type=event_type,
            normalized_email=email,
            client_ip=client_ip,
        )
    )


def _lock_rate_scope(
    session: Session, event_type: str, email: str, client_ip: str
) -> None:
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
        {"scope": f"{event_type}:{email}:{client_ip}"},
    )


def resend_verification(
    session: Session,
    email_service: EmailService,
    *,
    email: str,
    client_ip: str,
) -> None:
    normalized_email = normalize_email(email)
    now = utc_now()
    _lock_rate_scope(session, "verification_account", normalized_email, "*")
    _lock_rate_scope(session, "verification_ip", "*", client_ip)
    user = session.scalar(
        select(User).where(func.lower(User.email) == normalized_email).with_for_update()
    )
    if user is None:
        raise _error(
            "No unverified account is available for this email.",
            status.HTTP_400_BAD_REQUEST,
            "verification_not_available",
        )
    if user.email_verified_at is not None:
        raise _error("Email is already verified.", 400, "email_already_verified")
    latest = session.scalar(
        select(AuthenticationEvent.created_at)
        .where(
            AuthenticationEvent.event_type == "verification_resend",
            AuthenticationEvent.normalized_email == normalized_email,
        )
        .order_by(AuthenticationEvent.created_at.desc())
        .limit(1)
    )
    if latest is not None and latest > now - timedelta(seconds=60):
        raise _error(
            "Please wait before requesting another email.", 429, "resend_cooldown"
        )
    since = now - timedelta(hours=1)
    account_count = session.scalar(
        select(func.count())
        .select_from(AuthenticationEvent)
        .where(
            AuthenticationEvent.event_type == "verification_resend",
            AuthenticationEvent.normalized_email == normalized_email,
            AuthenticationEvent.created_at >= since,
        )
    )
    ip_count = session.scalar(
        select(func.count())
        .select_from(AuthenticationEvent)
        .where(
            AuthenticationEvent.event_type == "verification_resend",
            AuthenticationEvent.client_ip == client_ip,
            AuthenticationEvent.created_at >= since,
        )
    )
    if (account_count or 0) >= 5 or (ip_count or 0) >= 5:
        raise _error(
            "Too many verification requests. Try again later.", 429, "rate_limited"
        )

    session.execute(
        update(EmailVerificationToken)
        .where(
            EmailVerificationToken.user_id == user.id,
            EmailVerificationToken.consumed_at.is_(None),
            EmailVerificationToken.invalidated_at.is_(None),
        )
        .values(invalidated_at=now)
    )
    verification, raw_token = _new_verification_token(user)
    session.add(verification)
    _record_event(session, "verification_resend", normalized_email, client_ip)
    session.flush()
    try:
        email_service.send_verification_email(user.email, raw_token)
    except EmailDeliveryError:
        session.rollback()
        raise _error(
            "Verification email could not be sent. Please try again.",
            503,
            "email_delivery_unavailable",
        ) from None
    session.commit()


def login(
    session: Session,
    settings: Settings,
    *,
    email: str,
    password: str,
    keep_me_signed_in: bool,
    client_ip: str,
) -> tuple[User, str, str, object, object]:
    normalized_email = normalize_email(email)
    now = utc_now()
    cutoff = now - timedelta(minutes=15)
    _lock_rate_scope(session, "login_failure", normalized_email, client_ip)
    last_block = session.scalar(
        select(func.max(AuthenticationEvent.created_at)).where(
            AuthenticationEvent.event_type == "login_block",
            AuthenticationEvent.normalized_email == normalized_email,
            AuthenticationEvent.client_ip == client_ip,
        )
    )
    if last_block is not None and last_block >= cutoff:
        raise _error(
            "Too many login attempts. Try again later.", 429, "login_rate_limited"
        )
    failure_count = _event_count(
        session, "login_failure", normalized_email, client_ip, cutoff
    )
    user = session.scalar(
        select(User).where(func.lower(User.email) == normalized_email)
    )
    password_is_valid = verify_password(
        password, user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    )
    if user is None or not password_is_valid:
        _record_event(session, "login_failure", normalized_email, client_ip)
        if failure_count >= 4:
            _record_event(session, "login_block", normalized_email, client_ip)
        session.commit()
        raise _error(INVALID_CREDENTIALS_MESSAGE, 401, "invalid_credentials")
    if user.email_verified_at is None:
        raise _error("Email verification is required.", 403, "email_not_verified")
    if user.status is AccountStatus.DISABLED:
        raise _error("This account is disabled.", 403, "account_disabled")

    session.execute(
        delete(AuthenticationEvent).where(
            AuthenticationEvent.event_type.in_(["login_failure", "login_block"]),
            AuthenticationEvent.normalized_email == normalized_email,
            AuthenticationEvent.client_ip == client_ip,
        )
    )
    raw_refresh = generate_opaque_token()
    refresh_expires = now + timedelta(
        days=(
            settings.remembered_refresh_session_lifetime_days
            if keep_me_signed_in
            else settings.refresh_session_lifetime_days
        )
    )
    session.add(
        RefreshSession(
            user=user,
            token_hash=token_digest(raw_refresh),
            expires_at=refresh_expires,
        )
    )
    access_token, access_expires = create_access_token(user.id, settings)
    session.commit()
    return user, access_token, raw_refresh, access_expires, refresh_expires


def refresh_access_token(
    session: Session, settings: Settings, raw_token: str
) -> tuple[str, str, object, object]:
    now = utc_now()
    stored = session.scalar(
        select(RefreshSession)
        .where(RefreshSession.token_hash == token_digest(raw_token))
        .with_for_update()
    )
    if stored is None or not token_digest_matches(raw_token, stored.token_hash):
        raise _error("Refresh session is invalid.", 401, "refresh_token_invalid")
    if stored.revoked_at is not None:
        session.execute(
            update(RefreshSession)
            .where(
                RefreshSession.session_family_id == stored.session_family_id,
                RefreshSession.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        session.commit()
        raise _error("Refresh session is invalid.", 401, "refresh_token_reused")
    if stored.expires_at <= now:
        stored.revoked_at = now
        session.commit()
        raise _error("Refresh session has expired.", 401, "refresh_token_expired")
    user = session.get(User, stored.user_id)
    if user is None or user.status is AccountStatus.DISABLED:
        session.execute(
            update(RefreshSession)
            .where(
                RefreshSession.user_id == stored.user_id,
                RefreshSession.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        session.commit()
        raise _error("Refresh session is invalid.", 401, "refresh_token_invalid")

    raw_replacement = generate_opaque_token()
    replacement = RefreshSession(
        user_id=user.id,
        session_family_id=stored.session_family_id,
        token_hash=token_digest(raw_replacement),
        expires_at=stored.expires_at,
    )
    session.add(replacement)
    session.flush()
    stored.revoked_at = now
    stored.replaced_by_id = replacement.id
    access_token, access_expires = create_access_token(user.id, settings)
    session.commit()
    return access_token, raw_replacement, access_expires, replacement.expires_at


def logout_current(session: Session, raw_token: str | None) -> None:
    if raw_token:
        session.execute(
            update(RefreshSession)
            .where(
                RefreshSession.token_hash == token_digest(raw_token),
                RefreshSession.revoked_at.is_(None),
            )
            .values(revoked_at=utc_now())
        )
        session.commit()


def logout_all(session: Session, user: User) -> None:
    session.execute(
        update(RefreshSession)
        .where(
            RefreshSession.user_id == user.id,
            RefreshSession.revoked_at.is_(None),
        )
        .values(revoked_at=utc_now())
    )
    session.commit()


def forgot_password(
    session: Session,
    email_service: EmailService,
    *,
    email: str,
    client_ip: str,
) -> None:
    normalized_email = normalize_email(email)
    now = utc_now()
    _lock_rate_scope(session, "password_reset_request", normalized_email, client_ip)
    if (
        _event_count(
            session,
            "password_reset_request",
            normalized_email,
            client_ip,
            now - timedelta(hours=1),
        )
        >= 5
    ):
        raise _error(
            "Too many password-reset requests. Try again later.", 429, "rate_limited"
        )
    _record_event(session, "password_reset_request", normalized_email, client_ip)
    user = session.scalar(
        select(User).where(func.lower(User.email) == normalized_email)
    )
    if user is not None and user.status is AccountStatus.ACTIVE:
        raw_token = generate_opaque_token()
        session.add(
            PasswordResetToken(
                user=user,
                token_hash=token_digest(raw_token),
                expires_at=now + timedelta(minutes=30),
            )
        )
        session.flush()
        try:
            email_service.send_password_reset_email(user.email, raw_token)
        except EmailDeliveryError:
            session.rollback()
            return
    session.commit()


def reset_password(session: Session, raw_token: str, new_password: str) -> User:
    now = utc_now()
    token = session.scalar(
        select(PasswordResetToken)
        .where(PasswordResetToken.token_hash == token_digest(raw_token))
        .with_for_update()
    )
    if token is None or not token_digest_matches(raw_token, token.token_hash):
        raise _error("Password-reset token is invalid.", 400, "reset_token_invalid")
    if token.consumed_at is not None:
        raise _error(
            "Password-reset token is no longer valid.", 400, "reset_token_used"
        )
    if token.expires_at <= now:
        raise _error("Password-reset token has expired.", 400, "reset_token_expired")
    user = session.get(User, token.user_id)
    if user is None:
        raise _error("Password-reset token is invalid.", 400, "reset_token_invalid")
    _enforce_password_policy(new_password, user)
    token.consumed_at = now
    user.password_hash = hash_password(new_password)
    session.execute(
        update(RefreshSession)
        .where(
            RefreshSession.user_id == user.id,
            RefreshSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    session.commit()
    return user


def change_password(
    session: Session,
    user: User,
    *,
    current_password: str,
    new_password: str,
    current_refresh_token: str | None,
) -> None:
    if not verify_password(current_password, user.password_hash):
        raise _error("Current password is incorrect.", 400, "current_password_invalid")
    if verify_password(new_password, user.password_hash):
        raise _error(
            "New password must differ from the current password.",
            400,
            "password_reused",
        )
    _enforce_password_policy(new_password, user)
    keep_family_id: uuid.UUID | None = None
    if current_refresh_token:
        keep_family_id = session.scalar(
            select(RefreshSession.session_family_id).where(
                RefreshSession.user_id == user.id,
                RefreshSession.token_hash == token_digest(current_refresh_token),
                RefreshSession.revoked_at.is_(None),
                RefreshSession.expires_at > utc_now(),
            )
        )
    user.password_hash = hash_password(new_password)
    revoke_query = update(RefreshSession).where(
        RefreshSession.user_id == user.id,
        RefreshSession.revoked_at.is_(None),
    )
    if keep_family_id is not None:
        revoke_query = revoke_query.where(
            RefreshSession.session_family_id != keep_family_id
        )
    session.execute(revoke_query.values(revoked_at=utc_now()))
    session.commit()
