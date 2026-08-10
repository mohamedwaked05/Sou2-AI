"""Privacy-preserving digest generation for future tool-call auditing."""

import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

from pydantic import SecretStr


def hash_tool_arguments(arguments: Mapping[str, Any], secret: SecretStr) -> str:
    """HMAC-sign deterministic JSON and return only the lowercase hex digest."""
    secret_value = secret.get_secret_value()
    if not secret_value:
        raise ValueError("A non-empty server-side audit HMAC secret is required.")
    canonical = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hmac.new(secret_value.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
