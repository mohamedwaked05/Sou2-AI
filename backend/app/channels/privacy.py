"""Server-only customer identity protection helpers."""

import base64
import hashlib
import hmac
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings


class CustomerIdentityUnavailable(Exception):
    pass


_ENVELOPE_CONTEXT = b"sou2ai/customer-identity-envelope"
_CURRENT_VERSION = 2


def _secret(settings: Settings, name: str) -> str:
    value = getattr(settings, name)
    if value is None or not value.get_secret_value().strip():
        raise CustomerIdentityUnavailable("customer_identity_key_unavailable")
    return value.get_secret_value().strip()


def identity_hash(identity: str, settings: Settings) -> str:
    encryption_key = _secret(settings, "customer_identity_encryption_key")
    hmac_key = _secret(settings, "customer_identity_hmac_key")
    if len(encryption_key) < 32 or len(hmac_key) < 32 or encryption_key == hmac_key:
        raise CustomerIdentityUnavailable("customer_identity_key_invalid")
    return hmac.new(
        hmac_key.encode(),
        identity.encode(),
        hashlib.sha256,
    ).hexdigest()


def encrypt_identity(identity: str, settings: Settings) -> str:
    master = _secret(settings, "customer_identity_encryption_key").encode()
    hmac_key = _secret(settings, "customer_identity_hmac_key").encode()
    if len(master) < 32 or len(hmac_key) < 32 or master == hmac_key:
        raise CustomerIdentityUnavailable("customer_identity_key_invalid")
    encryption_key = hashlib.sha256(b"sou2ai-identity-aes-gcm\0" + master).digest()
    nonce = secrets.token_bytes(12)
    envelope = bytes([_CURRENT_VERSION]) + nonce
    ciphertext = AESGCM(encryption_key).encrypt(
        nonce, identity.encode(), _ENVELOPE_CONTEXT
    )
    return base64.urlsafe_b64encode(envelope + ciphertext).decode()


def decrypt_identity(ciphertext: str, settings: Settings) -> str:
    try:
        master = _secret(settings, "customer_identity_encryption_key").encode()
        if len(master) < 32:
            raise ValueError
        value = base64.b64decode(ciphertext.encode(), altchars=b"-_", validate=True)
        if len(value) < 1:
            raise ValueError
        if value[0] == 2:
            if len(value) < 1 + 12 + 16:
                raise ValueError
            nonce = value[1:13]
            encryption_key = hashlib.sha256(
                b"sou2ai-identity-aes-gcm\0" + master
            ).digest()
            return (
                AESGCM(encryption_key)
                .decrypt(nonce, value[13:], _ENVELOPE_CONTEXT)
                .decode()
            )
        if value[0] != 1 or len(value) < 1 + 16 + 32:
            raise ValueError
        envelope, supplied_tag = value[:-32], value[-32:]
        authentication_key = hmac.new(
            master, b"sou2ai-identity-authentication", hashlib.sha256
        ).digest()
        expected_tag = hmac.new(authentication_key, envelope, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied_tag, expected_tag):
            raise ValueError
        encryption_key = hmac.new(
            master, b"sou2ai-identity-encryption", hashlib.sha256
        ).digest()
        nonce, encrypted = envelope[1:17], envelope[17:]
        return bytes(
            left ^ right
            for left, right in zip(
                encrypted,
                _keystream(encryption_key, nonce, len(encrypted)),
                strict=True,
            )
        ).decode()
    except (InvalidTag, UnicodeDecodeError, ValueError) as exc:
        raise CustomerIdentityUnavailable("customer_identity_unavailable") from exc


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    blocks: list[bytes] = []
    counter = 0
    while sum(map(len, blocks)) < length:
        blocks.append(
            hmac.new(
                key,
                nonce + counter.to_bytes(8, "big"),
                hashlib.sha256,
            ).digest()
        )
        counter += 1
    return b"".join(blocks)[:length]


def masked_identity(identity: str) -> str:
    digits = "".join(character for character in identity if character.isdigit())
    return f"WhatsApp ••••{digits[-4:]}" if len(digits) >= 4 else "WhatsApp customer"
