"""Small deterministic metric helpers for the Milestone 13 retrieval dataset."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class EvaluationCase:
    identifier: str
    language: str
    expected_document: str
    expected_chunk_index: int


def metrics(
    cases: list[EvaluationCase], results: dict[str, list[tuple[str, int]]]
) -> dict[str, dict[str, float]]:
    """Compute Recall@5/@10, reciprocal rank, and execution failure rates."""
    grouped: dict[str, list[EvaluationCase]] = defaultdict(list)
    for case in cases:
        grouped[case.language].append(case)
    output: dict[str, dict[str, float]] = {}
    overall_rr: list[float] = []
    for language, group in grouped.items():
        recall5 = recall10 = failures = 0
        reciprocal_ranks: list[float] = []
        for case in group:
            ranked = results.get(case.identifier)
            if ranked is None:
                failures += 1
                reciprocal_ranks.append(0)
                continue
            target = (case.expected_document, case.expected_chunk_index)
            rank = next((i for i, hit in enumerate(ranked, 1) if hit == target), None)
            recall5 += int(rank is not None and rank <= 5)
            recall10 += int(rank is not None and rank <= 10)
            reciprocal_ranks.append(1 / rank if rank else 0)
        count = len(group)
        overall_rr.extend(reciprocal_ranks)
        output[language] = {
            "recall_at_5": recall5 / count,
            "recall_at_10": recall10 / count,
            "mrr": sum(reciprocal_ranks) / count,
            "execution_failure_rate": failures / count,
        }
    output["overall"] = {"mrr": sum(overall_rr) / len(overall_rr)}
    return output
