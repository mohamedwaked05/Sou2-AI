"""Tests for privacy-preserving canonical tool argument digests."""

from app.utils.argument_hashing import hash_tool_arguments
from pydantic import SecretStr


def test_semantically_identical_dictionary_order_has_stable_hmac() -> None:
    secret = SecretStr("server-only-test-secret")
    first = hash_tool_arguments({"city": "Beirut", "page": 2}, secret)
    second = hash_tool_arguments({"page": 2, "city": "Beirut"}, secret)

    assert first == second
    assert len(first) == 64
    assert "Beirut" not in first


def test_different_secret_changes_digest() -> None:
    arguments = {"value": 1}
    assert hash_tool_arguments(arguments, SecretStr("one")) != hash_tool_arguments(
        arguments, SecretStr("two")
    )
