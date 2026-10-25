"""Regression checks for the container RQ command."""

from pathlib import Path


def test_compose_uses_rq_cli_for_the_knowledge_worker() -> None:
    compose = (Path(__file__).parents[2] / "compose.yaml").read_text(encoding="utf-8")
    assert "python -m rq" not in compose
    assert (
        "rq worker --with-scheduler --url redis://redis:6379/0 "
        "--worker-class app.worker.runtime.PrivacySafeWorker" in compose
    )
    assert "${KNOWLEDGE_QUEUE_NAME:-knowledge}" in compose


def test_compose_worker_shares_storage_and_gemini_embedding_budget() -> None:
    compose = (Path(__file__).parents[2] / "compose.yaml").read_text(encoding="utf-8")
    assert "KNOWLEDGE_STORAGE_ROOT: /app/data/knowledge" in compose
    assert "- ./data:/app/data" in compose
    assert (
        "OLLAMA_BASE_URL: ${OLLAMA_BASE_URL:-http://host.docker.internal:11434}"
        in compose
    )
    assert "EMBEDDING_MODEL: ${EMBEDDING_MODEL:-gemini-embedding-001}" in compose
    assert "EMBEDDING_PROVIDER: ${EMBEDDING_PROVIDER:-gemini}" in compose
    assert (
        "KNOWLEDGE_WORKER_TIMEOUT_SECONDS: ${KNOWLEDGE_WORKER_TIMEOUT_SECONDS:-900}"
        in compose
    )
    assert "OLLAMA_EMBEDDING_MODEL" not in compose
