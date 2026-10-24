"""Contract tests for the local embedding adapter without Ollama I/O."""

import json
import math

import httpx
import pytest
from app.rag.embeddings import (
    EMBEDDING_DIMENSIONS,
    EmbeddingProviderError,
    GeminiEmbeddingProvider,
    OllamaEmbeddingProvider,
    create_embedding_provider,
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


@pytest.mark.parametrize("task", ["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"])
def test_gemini_contract_preserves_order_dimension_and_normalizes(task: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert (
            str(request.url)
            == "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:batchEmbedContents"
        )
        assert request.headers["x-goog-api-key"] == "private-test-key"
        body = json.loads(request.content)
        assert [r["content"]["parts"][0]["text"] for r in body["requests"]] == [
            "first",
            "second",
        ]
        assert all(
            r["outputDimensionality"] == 1024 and r["taskType"] == task
            for r in body["requests"]
        )
        return httpx.Response(
            200,
            json={
                "embeddings": [
                    {"values": [3, 4] + [0] * 1022},
                    {"values": [0, 2] + [0] * 1022},
                ]
            },
        )

    provider = GeminiEmbeddingProvider(
        api_key="private-test-key",
        model="gemini-embedding-001",
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    result = provider.embed(["first", "second"], task_type=task)
    assert result.model == "gemini-embedding-001"
    assert result.vectors[0][:2] == (0.6, 0.8)
    assert result.vectors[1][:2] == (0.0, 1.0)
    assert all(
        len(v) == 1024 and math.isclose(math.hypot(*v), 1) for v in result.vectors
    )


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"embeddings": []}, "embedding_output_count"),
        ({"embeddings": [{"values": [1]}]}, "embedding_dimension"),
        ({"embeddings": [{"values": [0] * 1024}]}, "embedding_invalid_values"),
        ({"embeddings": [{"values": [True] * 1024}]}, "embedding_invalid_values"),
        ({"embeddings": [{"values": [math.nan] * 1024}]}, "embedding_invalid_values"),
        ({"embeddings": [{"values": [math.inf] * 1024}]}, "embedding_invalid_values"),
        ([], "embedding_invalid_response"),
    ],
)
def test_gemini_rejects_unusable_vectors(payload, code: str) -> None:
    provider = GeminiEmbeddingProvider(
        api_key="secret",
        model="gemini-embedding-001",
        timeout_seconds=1,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=json.dumps(payload, allow_nan=True))
        ),
    )
    with pytest.raises(EmbeddingProviderError, match=code):
        provider.embed(["question"])


@pytest.mark.parametrize(
    "status,retryable", [(401, False), (404, False), (429, True), (503, True)]
)
def test_gemini_errors_are_safe_and_correctly_retryable(
    status: int, retryable: bool
) -> None:
    provider = GeminiEmbeddingProvider(
        api_key="secret",
        model="gemini-embedding-001",
        timeout_seconds=1,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, text="sensitive-provider-body")
        ),
    )
    with pytest.raises(EmbeddingProviderError) as caught:
        provider.embed(["private document"])
    assert caught.value.retryable is retryable
    assert "sensitive" not in str(caught.value)


def test_gemini_factory_requires_matching_configuration() -> None:
    from app.core.config import Settings

    settings = Settings(
        _env_file=None,
        embedding_provider="gemini",
        embedding_model="gemini-embedding-001",
        gemini_api_key="test",
    )
    assert isinstance(create_embedding_provider(settings), GeminiEmbeddingProvider)
    with pytest.raises(ValueError, match="EMBEDDING_MODEL"):
        Settings(_env_file=None, embedding_provider="gemini", gemini_api_key="test")
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        Settings(
            _env_file=None,
            embedding_provider="gemini",
            embedding_model="gemini-embedding-001",
        )
    invalid_request = _provider(
        httpx.MockTransport(lambda _: httpx.Response(400, content=b"not-json"))
    )
    with pytest.raises(EmbeddingProviderError, match="embedding_http_error") as caught:
        invalid_request.embed(["a"])
    assert not caught.value.retryable
