"""Provider-neutral embeddings with a local Ollama implementation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx

from app.core.config import Settings

EMBEDDING_DIMENSIONS = 1024


class EmbeddingProviderError(Exception):
    """Safe provider error.  Its code is suitable for internal logging only."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: tuple[tuple[float, ...], ...]
    model: str


@runtime_checkable
class EmbeddingProvider(Protocol):
    model: str

    def embed(self, texts: list[str]) -> EmbeddingResult: ...


class OllamaEmbeddingProvider:
    """Ollama `/api/embed` adapter; it deliberately never logs provider payloads."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def embed(self, texts: list[str]) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=(), model=self.model)
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = client.post(
                    "/api/embed", json={"model": self.model, "input": texts}
                )
        except httpx.TimeoutException:
            raise EmbeddingProviderError("embedding_timeout", retryable=True) from None
        except httpx.ConnectError:
            raise EmbeddingProviderError(
                "embedding_unavailable", retryable=True
            ) from None
        except httpx.RequestError:
            raise EmbeddingProviderError(
                "embedding_transport_error", retryable=True
            ) from None

        if response.status_code >= 400:
            if response.status_code in {408, 425, 429} or response.status_code >= 500:
                raise EmbeddingProviderError("embedding_http_error", retryable=True)
            try:
                payload = response.json()
            except ValueError:
                payload = None
            missing = (
                response.status_code == 404
                and isinstance(payload, dict)
                and "model" in str(payload.get("error", "")).casefold()
            )
            raise EmbeddingProviderError(
                "embedding_model_missing" if missing else "embedding_http_error",
                retryable=False,
            )
        try:
            payload = response.json()
        except ValueError:
            raise EmbeddingProviderError(
                "embedding_invalid_response", retryable=False
            ) from None
        if not isinstance(payload, dict) or not isinstance(
            payload.get("embeddings"), list
        ):
            raise EmbeddingProviderError("embedding_invalid_response", retryable=False)
        raw_vectors = payload["embeddings"]
        if len(raw_vectors) != len(texts):
            raise EmbeddingProviderError("embedding_output_count", retryable=False)
        vectors: list[tuple[float, ...]] = []
        for vector in raw_vectors:
            if not isinstance(vector, list) or len(vector) != EMBEDDING_DIMENSIONS:
                raise EmbeddingProviderError("embedding_dimension", retryable=False)
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in vector
            ):
                raise EmbeddingProviderError(
                    "embedding_invalid_values", retryable=False
                )
            vectors.append(tuple(float(value) for value in vector))
        return EmbeddingResult(vectors=tuple(vectors), model=self.model)


def create_embedding_provider(
    settings: Settings, *, transport: httpx.BaseTransport | None = None
) -> EmbeddingProvider:
    """Create the configured embedding adapter without performing I/O."""
    if settings.embedding_provider != "ollama":
        raise ValueError("Unsupported embedding provider.")
    return OllamaEmbeddingProvider(
        base_url=settings.ollama_base_url,
        model=settings.embedding_model,
        timeout_seconds=settings.ollama_request_timeout_seconds,
        transport=transport,
    )


def embed_batched(
    provider: EmbeddingProvider, texts: list[str], batch_size: int
) -> list[list[float]]:
    """Embed ordered input in bounded batches and preserve its order."""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        result = provider.embed(texts[start : start + batch_size])
        vectors.extend([list(vector) for vector in result.vectors])
    if len(vectors) != len(texts):
        raise EmbeddingProviderError("embedding_output_count", retryable=False)
    return vectors
