import json
from pathlib import Path

from app.rag.evaluation import EvaluationCase, metrics


def test_metrics_are_deterministic_and_include_failures() -> None:
    cases = [
        EvaluationCase("a", "english", "doc", 0),
        EvaluationCase("b", "english", "doc", 1),
    ]
    result = metrics(cases, {"a": [("doc", 0)]})
    assert result["english"] == {
        "recall_at_5": 0.5,
        "recall_at_10": 0.5,
        "mrr": 0.5,
        "execution_failure_rate": 0.5,
    }
    assert result["overall"]["mrr"] == 0.5


def test_evaluation_fixture_has_targets_and_semantic_distractors() -> None:
    fixture = Path(__file__).parents[1] / "evaluations" / "milestone_13_retrieval.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    assert len(payload["cases"]) == 30
    assert len(payload["documents"]) >= 16
    assert len({item["tenant_id"] for item in payload["documents"]}) == 2
