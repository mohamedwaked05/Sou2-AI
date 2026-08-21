"""Regression checks for the container RQ command."""

from pathlib import Path


def test_compose_uses_rq_cli_for_the_knowledge_worker() -> None:
    compose = (Path(__file__).parents[2] / "compose.yaml").read_text(encoding="utf-8")
    assert "python -m rq" not in compose
    assert "rq worker --url ${REDIS_URL:-redis://redis:6379/0}" in compose
    assert "${KNOWLEDGE_QUEUE_NAME:-knowledge}" in compose
