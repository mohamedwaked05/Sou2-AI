"""Bounded retrieval embeddings with validated, model-specific vectors."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

import httpx

from app.core.config import Settings

EMBEDDING_DIMENSIONS = 1024
EmbeddingTask = Literal["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]


class EmbeddingProviderError(Exception):
    """Safe provider error.  Its code is suitable for internal logging only."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        retry_after_seconds: int | None = None,
        http_status: int | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.http_status = http_status
        super().__init__(code)


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: tuple[tuple[float, ...], ...]
    model: str


@runtime_checkable
class EmbeddingProvider(Protocol):
    model: str

    def embed(
        self, texts: list[str], *, task_type: EmbeddingTask = "RETRIEVAL_DOCUMENT"
    ) -> EmbeddingResult: ...


class GeminiEmbeddingProvider:
    """Gemini text embeddings; credentials and provider bodies stay private."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if model != "gemini-embedding-001":
            raise ValueError("Unsupported Gemini text embedding model.")
        self.model = model
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    @staticmethod
    def _retry_after(response: httpx.Response) -> int | None:
        """Read only a bounded delay; never retain provider messages or payloads."""
        candidates = [response.headers.get("retry-after")]
        try:
            payload = response.json()
            details = payload.get("error", {}).get("details", [])
            candidates.extend(
                item.get("retryDelay", "").removesuffix("s")
                for item in details
                if isinstance(item, dict)
                and item.get("@type") == "type.googleapis.com/google.rpc.RetryInfo"
            )
        except ValueError, TypeError, AttributeError:
            pass
        for candidate in candidates:
            try:
                seconds = float(candidate)
            except ValueError, TypeError:
                continue
            if math.isfinite(seconds) and seconds >= 0:
                return max(1, math.ceil(seconds))
        return None

    def embed(
        self, texts: list[str], *, task_type: EmbeddingTask = "RETRIEVAL_DOCUMENT"
    ) -> EmbeddingResult:
        if task_type not in {"RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"}:
            raise ValueError("Unsupported embedding task.")
        if not texts:
            return EmbeddingResult(vectors=(), model=self.model)
        # The synchronous batch endpoint accepts at most 100 individual inputs.
        if len(texts) > 100:
            raise ValueError("Embedding batch exceeds 100 inputs.")
        model = f"models/{self.model}"
        try:
            with httpx.Client(
                base_url="https://generativelanguage.googleapis.com/v1beta/",
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = client.post(
                    f"{model}:batchEmbedContents",
                    headers={"x-goog-api-key": self._api_key},
                    json={
                        "requests": [
                            {
                                "model": model,
                                "content": {"parts": [{"text": text}]},
                                "taskType": task_type,
                                "outputDimensionality": EMBEDDING_DIMENSIONS,
                            }
                            for text in texts
                        ]
                    },
                )
        except httpx.TimeoutException:
            raise EmbeddingProviderError("embedding_timeout", retryable=True) from None
        except httpx.RequestError:
            raise EmbeddingProviderError(
                "embedding_unavailable", retryable=True
            ) from None
        if response.status_code >= 400:
            limited = response.status_code == 429
            delay = self._retry_after(response)
            if limited and delay is None:
                delay = 60
            raise EmbeddingProviderError(
                "embedding_model_missing"
                if response.status_code == 404
                else "embedding_rate_limited"
                if limited
                else "embedding_http_error",
                retryable=(
                    response.status_code in {408, 425, 429}
                    or response.status_code >= 500
                )
                and (delay is None or delay <= 86400),
                retry_after_seconds=delay,
                http_status=response.status_code,
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
        if len(payload["embeddings"]) != len(texts):
            raise EmbeddingProviderError("embedding_output_count", retryable=False)
        vectors = []
        for item in payload["embeddings"]:
            vector = item.get("values") if isinstance(item, dict) else None
            if not isinstance(vector, list) or len(vector) != EMBEDDING_DIMENSIONS:
                raise EmbeddingProviderError("embedding_dimension", retryable=False)
            if any(
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
                for v in vector
            ):
                raise EmbeddingProviderError(
                    "embedding_invalid_values", retryable=False
                )
            norm = math.hypot(*vector)
            if not norm or not math.isfinite(norm):
                raise EmbeddingProviderError(
                    "embedding_invalid_values", retryable=False
                )
            # Truncated gemini-embedding-001 vectors require L2 normalization.
            vectors.append(tuple(v / norm for v in vector))
        return EmbeddingResult(vectors=tuple(vectors), model=self.model)


def create_embedding_provider(
    settings: Settings, *, transport: httpx.BaseTransport | None = None
) -> EmbeddingProvider:
    """Create the configured embedding adapter without performing I/O."""
    if settings.embedding_provider != "gemini":
        raise ValueError("Gemini is the required embedding provider.")
    if (
        settings.gemini_api_key is None
        or not settings.gemini_api_key.get_secret_value().strip()
    ):
        raise ValueError("Gemini embedding credentials are required.")
    return GeminiEmbeddingProvider(
        api_key=settings.gemini_api_key.get_secret_value(),
        model=settings.embedding_model,
        timeout_seconds=settings.embedding_request_timeout_seconds,
        transport=transport,
    )


def embed_batched(
    provider: EmbeddingProvider,
    texts: list[str],
    batch_size: int,
    *,
    task_type: EmbeddingTask = "RETRIEVAL_DOCUMENT",
    deadline: float | None = None,
) -> list[list[float]]:
    """Embed ordered input in bounded batches and preserve its order."""
    vectors: list[list[float]] = []
    if batch_size < 1:
        raise ValueError("Embedding batch size must be positive.")
    if isinstance(provider, GeminiEmbeddingProvider):
        batch_size = min(batch_size, 100)
    for start in range(0, len(texts), batch_size):
        if deadline is not None and time.monotonic() >= deadline:
            raise EmbeddingProviderError("embedding_timeout", retryable=True)
        for attempt in range(3):
            try:
                result = provider.embed(
                    texts[start : start + batch_size], task_type=task_type
                )
                break
            except EmbeddingProviderError as exc:
                delay = exc.retry_after_seconds or 2 ** (attempt + 1)
                if (
                    deadline is None
                    or not exc.retryable
                    or attempt == 2
                    or time.monotonic() + delay >= deadline
                ):
                    raise
                time.sleep(delay)
        if deadline is not None and time.monotonic() >= deadline:
            raise EmbeddingProviderError("embedding_timeout", retryable=True)
        if result.model != provider.model:
            raise EmbeddingProviderError("embedding_model_mismatch", retryable=False)
        vectors.extend([list(vector) for vector in result.vectors])
    if len(vectors) != len(texts):
        raise EmbeddingProviderError("embedding_output_count", retryable=False)
    return vectors
