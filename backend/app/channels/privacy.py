"""Server-only customer identity protection helpers."""

import base64
import hashlib
import hmac
import secrets

from app.core.config import Settings


class CustomerIdentityUnavailable(Exception):
    pass


def _secret(settings: Settings, name: str) -> str:
    value = getattr(settings, name)
    if value is None or not value.get_secret_value().strip():
        raise CustomerIdentityUnavailable("customer_identity_key_unavailable")
    return value.get_secret_value().strip()


def identity_hash(identity: str, settings: Settings) -> str:
    return hmac.new(
        _secret(settings, "customer_identity_hmac_key").encode(),
        identity.encode(),
        hashlib.sha256,
    ).hexdigest()


def encrypt_identity(identity: str, settings: Settings) -> str:
    master = _secret(settings, "customer_identity_encryption_key").encode()
    if len(master) < 32:
        raise CustomerIdentityUnavailable("customer_identity_key_invalid")
    encryption_key = hmac.new(
        master, b"sou2ai-identity-encryption", hashlib.sha256
    ).digest()
    authentication_key = hmac.new(
        master, b"sou2ai-identity-authentication", hashlib.sha256
    ).digest()
    nonce = secrets.token_bytes(16)
    plaintext = identity.encode()
    stream = _keystream(encryption_key, nonce, len(plaintext))
    ciphertext = bytes(
        left ^ right for left, right in zip(plaintext, stream, strict=True)
    )
    envelope = b"\x01" + nonce + ciphertext
    tag = hmac.new(authentication_key, envelope, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(envelope + tag).decode()


def decrypt_identity(ciphertext: str, settings: Settings) -> str:
    try:
        master = _secret(settings, "customer_identity_encryption_key").encode()
        if len(master) < 32:
            raise ValueError
        value = base64.b64decode(ciphertext.encode(), altchars=b"-_", validate=True)
        if len(value) < 50 or value[0] != 1:
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
        stream = _keystream(encryption_key, nonce, len(encrypted))
        return bytes(
            left ^ right for left, right in zip(encrypted, stream, strict=True)
        ).decode()
    except (UnicodeDecodeError, ValueError) as exc:
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
