"""Contract tests for Gemini embeddings without external provider I/O."""

import json
import math

import httpx
import pytest
from app.rag.embeddings import (
    EMBEDDING_DIMENSIONS,
    EmbeddingProviderError,
    EmbeddingResult,
    GeminiEmbeddingProvider,
    create_embedding_provider,
    embed_batched,
)


def _vector(value: float = 0.1) -> list[float]:
    return [value] * EMBEDDING_DIMENSIONS


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
        Settings(_env_file=None, embedding_model="bge-m3")
    with pytest.raises(ValueError, match="embedding_provider"):
        Settings(_env_file=None, embedding_provider="ollama")
    with pytest.raises(ValueError, match="credentials"):
        create_embedding_provider(Settings(_env_file=None, gemini_api_key=None))


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
def test_transport_failures_are_retryable_and_sanitized(failure):
    def fail(request):
        raise failure("private transport detail")

    provider = GeminiEmbeddingProvider(
        api_key="private-test-key",
        model="gemini-embedding-001",
        timeout_seconds=1,
        transport=httpx.MockTransport(fail),
    )
    with pytest.raises(EmbeddingProviderError) as caught:
        provider.embed(["private document"])
    assert caught.value.retryable
    assert "private" not in str(caught.value)


def test_embedding_deadline_stops_before_another_provider_batch(monkeypatch):
    from unittest.mock import Mock

    from app.rag import embeddings

    provider = Mock(model="gemini-embedding-001")
    provider.embed.return_value = embeddings.EmbeddingResult(
        vectors=(tuple(_vector()),), model=provider.model
    )
    monkeypatch.setattr(embeddings.time, "monotonic", Mock(side_effect=[0, 2, 3]))
    with pytest.raises(EmbeddingProviderError, match="embedding_timeout"):
        embed_batched(provider, ["first", "second"], 1, deadline=2.5)
    assert provider.embed.call_count == 1


def test_maximum_scope_fits_even_at_the_per_request_timeout(monkeypatch):
    from unittest.mock import Mock

    from app.core.config import Settings
    from app.rag import embeddings

    configured = Settings(_env_file=None)
    clock = [0.0]
    provider = Mock(model=configured.embedding_model)

    def embed(texts, **kwargs):
        clock[0] += configured.embedding_request_timeout_seconds
        return embeddings.EmbeddingResult(
            vectors=tuple(tuple(_vector()) for _ in texts), model=provider.model
        )

    provider.embed.side_effect = embed
    monkeypatch.setattr(embeddings.time, "monotonic", lambda: clock[0])
    vectors = embed_batched(
        provider,
        ["chunk"] * 500,
        configured.embedding_batch_size,
        deadline=configured.knowledge_worker_timeout_seconds - 30,
    )
    assert len(vectors) == 500
    assert provider.embed.call_count == 32
    assert clock[0] == 640


@pytest.mark.parametrize(
    "header,details,expected",
    [
        ("2.2", [], 3),
        (
            None,
            [
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "15647s",
                }
            ],
            15647,
        ),
        (None, [], 60),
    ],
)
def test_gemini_quota_delays_are_sanitized_and_retained(header, details, expected):
    provider = GeminiEmbeddingProvider(
        api_key="private-test-key",
        model="gemini-embedding-001",
        timeout_seconds=1,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                429,
                headers={"retry-after": header} if header else {},
                json={
                    "error": {"message": "private provider text", "details": details}
                },
            )
        ),
    )
    with pytest.raises(EmbeddingProviderError) as caught:
        provider.embed(["document"])
    assert caught.value.code == "embedding_rate_limited"
    assert caught.value.http_status == 429
    assert caught.value.retry_after_seconds == expected
    assert "private" not in str(caught.value)


def test_quota_retry_keeps_completed_batches_in_the_current_attempt(monkeypatch):
    from unittest.mock import Mock

    from app.rag import embeddings

    clock = [0.0]
    provider = Mock(model="gemini-embedding-001")
    result = EmbeddingResult(vectors=(tuple(_vector()),), model=provider.model)
    provider.embed.side_effect = [
        result,
        EmbeddingProviderError(
            "embedding_rate_limited", retryable=True, retry_after_seconds=5
        ),
        result,
    ]
    monkeypatch.setattr(embeddings.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        embeddings.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay)
    )
    assert len(embed_batched(provider, ["first", "second"], 1, deadline=100)) == 2
    assert [call.args[0] for call in provider.embed.call_args_list] == [
        ["first"],
        ["second"],
        ["second"],
    ]
    assert clock[0] == 5


def test_long_provider_delay_is_deferred_outside_the_running_job(monkeypatch):
    from unittest.mock import Mock

    from app.rag import embeddings

    provider = Mock(model="gemini-embedding-001")
    error = EmbeddingProviderError(
        "embedding_rate_limited", retryable=True, retry_after_seconds=15647
    )
    provider.embed.side_effect = error
    monkeypatch.setattr(embeddings.time, "monotonic", lambda: 0)
    sleep = Mock()
    monkeypatch.setattr(embeddings.time, "sleep", sleep)
    with pytest.raises(EmbeddingProviderError) as caught:
        embed_batched(provider, ["chunk"], 16, deadline=870)
    assert caught.value is error
    provider.embed.assert_called_once()
    sleep.assert_not_called()


def test_excessive_provider_delay_requires_review_instead_of_early_retry():
    provider = GeminiEmbeddingProvider(
        api_key="private-test-key",
        model="gemini-embedding-001",
        timeout_seconds=1,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(429, headers={"retry-after": "86401"})
        ),
    )
    with pytest.raises(EmbeddingProviderError) as caught:
        provider.embed(["document"])
    assert not caught.value.retryable
    assert caught.value.retry_after_seconds == 86401


def test_api_lifespan_rejects_missing_embedding_credentials_without_provider_io():
    from app.core.config import Settings
    from app.main import create_app
    from fastapi.testclient import TestClient

    configured = Settings(
        _env_file=None,
        environment="testing",
        gemini_api_key=None,
        owner_chat_provider="mock",
        source_mapping_provider="mock",
        conversation_summary_provider="mock",
        customer_chat_provider="mock",
    )
    with pytest.raises(RuntimeError, match="release_embedding_credentials_missing"):
        with TestClient(create_app(configured)):
            pytest.fail("Invalid embedding configuration must not start")


def test_release_embedding_checks_are_local_and_reject_invalid_settings(monkeypatch):
    from unittest.mock import Mock

    from app.core.config import Settings
    from app.core.runtime import (
        validate_embedding_configuration,
        worker_configuration_identity,
    )

    monkeypatch.setattr(
        httpx.Client, "post", Mock(side_effect=AssertionError("no startup API calls"))
    )
    configured = Settings(_env_file=None, gemini_api_key="private-test-key")
    validate_embedding_configuration(configured)
    assert configured.embedding_provider == "gemini"
    for change, code in (
        ({"gemini_api_key": None}, "credentials_missing"),
        ({"embedding_provider": "ollama"}, "model_invalid"),
        ({"embedding_model": "wrong-model"}, "model_invalid"),
        ({"embedding_batch_size": 0}, "execution_budget_invalid"),
        ({"embedding_batch_size": 101}, "execution_budget_invalid"),
        ({"embedding_request_timeout_seconds": 31}, "execution_budget_invalid"),
        ({"knowledge_worker_timeout_seconds": 120}, "execution_budget_invalid"),
    ):
        with pytest.raises(RuntimeError, match=code) as caught:
            validate_embedding_configuration(configured.model_copy(update=change))
        assert "private-test-key" not in str(caught.value)
    assert worker_configuration_identity(configured) != worker_configuration_identity(
        configured.model_copy(update={"embedding_batch_size": 20})
    )


def test_supported_upload_scope_must_fit_the_configured_job_timeout():
    from app.core.config import Settings

    with pytest.raises(ValueError, match="KNOWLEDGE_WORKER_TIMEOUT_SECONDS"):
        Settings(_env_file=None, knowledge_worker_timeout_seconds=120)
    with pytest.raises(ValueError, match="embedding_batch_size"):
        Settings(_env_file=None, embedding_batch_size=101)


def test_storage_validation_checks_the_actual_vector_dimension(migration_engine):
    from unittest.mock import Mock

    from app.database.session import validate_embedding_storage

    with migration_engine.connect() as connection:
        validate_embedding_storage(connection)
    with pytest.raises(RuntimeError, match="embedding_storage_dimension_mismatch"):
        validate_embedding_storage(Mock(scalar=Mock(return_value="vector(768)")))
