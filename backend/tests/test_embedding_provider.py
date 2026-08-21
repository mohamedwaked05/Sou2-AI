"""Contract tests for the local embedding adapter without Ollama I/O."""

import json
import math

import httpx
import pytest
from app.rag.embeddings import (
    EMBEDDING_DIMENSIONS,
    EmbeddingProviderError,
    OllamaEmbeddingProvider,
    embed_batched,
)


def _vector(value: float = 0.1) -> list[float]:
    return [value] * EMBEDDING_DIMENSIONS


def _provider(handler: httpx.MockTransport) -> OllamaEmbeddingProvider:
    return OllamaEmbeddingProvider(
        base_url="http://ollama.invalid",
        model="bge-m3",
        timeout_seconds=1,
        transport=handler,
    )


def test_batches_inputs_and_uses_embed_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        count = len(json.loads(request.content)["input"])
        return httpx.Response(200, json={"embeddings": [_vector()] * count})

    vectors = embed_batched(_provider(httpx.MockTransport(handler)), ["a", "b", "c"], 2)
    assert len(vectors) == 3
    assert [request.url.path for request in requests] == ["/api/embed", "/api/embed"]


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"embeddings": [_vector()]}, "embedding_output_count"),
        ({"embeddings": [[0.1], [0.1]]}, "embedding_dimension"),
        (
            {"embeddings": [["x"] * EMBEDDING_DIMENSIONS] * 2},
            "embedding_invalid_values",
        ),
        (
            {"embeddings": [[math.nan] * EMBEDDING_DIMENSIONS] * 2},
            "embedding_invalid_values",
        ),
        (
            {"embeddings": [[math.inf] * EMBEDDING_DIMENSIONS] * 2},
            "embedding_invalid_values",
        ),
    ],
)
def test_rejects_invalid_contracts(payload: object, code: str) -> None:
    provider = _provider(
        httpx.MockTransport(
            lambda _: httpx.Response(200, content=json.dumps(payload, allow_nan=True))
        )
    )
    with pytest.raises(EmbeddingProviderError, match=code):
        provider.embed(["a", "b"])


def test_timeout_is_normalized_retryable() -> None:
    def timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout")

    with pytest.raises(EmbeddingProviderError, match="embedding_timeout") as caught:
        _provider(httpx.MockTransport(timeout)).embed(["a"])
    assert caught.value.retryable


@pytest.mark.parametrize("status_code", [408, 425, 429, 500, 503])
def test_temporary_http_failures_are_retryable_even_with_invalid_json(
    status_code: int,
) -> None:
    provider = _provider(
        httpx.MockTransport(lambda _: httpx.Response(status_code, content=b"not-json"))
    )
    with pytest.raises(EmbeddingProviderError, match="embedding_http_error") as caught:
        provider.embed(["a"])
    assert caught.value.retryable


def test_missing_model_and_other_client_errors_are_not_retryable() -> None:
    missing = _provider(
        httpx.MockTransport(
            lambda _: httpx.Response(404, json={"error": "model not found"})
        )
    )
    with pytest.raises(
        EmbeddingProviderError, match="embedding_model_missing"
    ) as caught:
        missing.embed(["a"])
    assert not caught.value.retryable
    invalid_request = _provider(
        httpx.MockTransport(lambda _: httpx.Response(400, content=b"not-json"))
    )
    with pytest.raises(EmbeddingProviderError, match="embedding_http_error") as caught:
        invalid_request.embed(["a"])
    assert not caught.value.retryable
