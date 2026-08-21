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
