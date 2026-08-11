"""Credential, token, and access-token security primitives."""

import hashlib
import hmac
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from jwt import InvalidTokenError
from pwdlib import PasswordHash

from app.core.config import Settings

password_hash = PasswordHash.recommended()


def utc_now() -> datetime:
    return datetime.now(UTC)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, encoded_hash: str) -> bool:
    try:
        return password_hash.verify(password, encoded_hash)
    except Exception:
        return False


def password_policy_violations(
    password: str, *, first_name: str, last_name: str, email: str
) -> list[str]:
    """Return stable policy codes without ever including the submitted password."""
    violations: list[str] = []
    if len(password) < 8:
        violations.append("minimum_length")
    if len(password) > 128:
        violations.append("maximum_length")
    if not any(character.isupper() for character in password):
        violations.append("uppercase_required")
    if not any(character.islower() for character in password):
        violations.append("lowercase_required")
    if not any(character.isdigit() for character in password):
        violations.append("number_required")
    if not any(not character.isalnum() for character in password):
        violations.append("special_character_required")

    folded_password = password.casefold()
    for code, value in (("first_name", first_name), ("last_name", last_name)):
        meaningful_name = value.strip().casefold()
        if meaningful_name and meaningful_name in folded_password:
            violations.append(f"contains_{code}")

    local_part = normalize_email(email).partition("@")[0]
    meaningful_local_part = "".join(re.findall(r"[a-z0-9]+", local_part.casefold()))
    if len(meaningful_local_part) >= 3 and meaningful_local_part in folded_password:
        violations.append("contains_email_local_part")
    return violations


def generate_opaque_token() -> str:
    return secrets.token_urlsafe(48)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_digest_matches(token: str, stored_digest: str) -> bool:
    return hmac.compare_digest(token_digest(token), stored_digest)


def create_access_token(user_id: uuid.UUID, settings: Settings) -> tuple[str, datetime]:
    issued_at = utc_now()
    expires_at = issued_at + timedelta(minutes=settings.access_token_lifetime_minutes)
    payload = {
        "sub": str(user_id),
        "type": "access",
        "iat": issued_at,
        "exp": expires_at,
        "iss": settings.access_token_issuer,
        "aud": settings.access_token_audience,
    }
    token = jwt.encode(
        payload,
        settings.access_token_secret.get_secret_value(),
        algorithm=settings.access_token_algorithm,
    )
    return token, expires_at


def decode_access_token(token: str, settings: Settings) -> uuid.UUID:
    try:
        payload = jwt.decode(
            token,
            settings.access_token_secret.get_secret_value(),
            algorithms=[settings.access_token_algorithm],
            audience=settings.access_token_audience,
            issuer=settings.access_token_issuer,
            options={"require": ["sub", "type", "iat", "exp", "iss", "aud"]},
        )
        if payload["type"] != "access":
            raise InvalidTokenError
        return uuid.UUID(payload["sub"])
    except (InvalidTokenError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid access token.") from exc
