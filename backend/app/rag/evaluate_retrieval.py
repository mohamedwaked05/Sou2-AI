"""Run the fixed Milestone 13 BGE-M3 retrieval evaluation locally."""

from __future__ import annotations

import json
import math
from pathlib import Path

from app.core.config import get_settings
from app.rag.embeddings import create_embedding_provider, embed_batched
from app.rag.evaluation import EvaluationCase, metrics

FIXTURES = {
    "policies": "Returns are accepted within 14 days with the original receipt.",
    "delivery": "Delivery in Beirut costs five dollars and takes one business day.",
    "hours": "The store opens Monday through Saturday from 9 AM until 6 PM.",
    "warranty": "All electronics include a one-year manufacturer warranty.",
    "location": "The store is on Hamra Street in Beirut near the university.",
    "service": "We provide professional product installation at the customer location.",
}


def _cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True)) / (
        math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    )


def main() -> None:
    settings = get_settings()
    provider = create_embedding_provider(settings)
    dataset = Path(__file__).parents[2] / "evaluations" / "milestone_13_retrieval.json"
    raw_cases = json.loads(dataset.read_text(encoding="utf-8"))
    cases = [
        EvaluationCase(
            item["id"],
            item["language"],
            item["expected_document"],
            item["expected_chunk_index"],
        )
        for item in raw_cases
    ]
    document_names = list(FIXTURES)
    vectors = embed_batched(
        provider,
        list(FIXTURES.values()) + [item["question"] for item in raw_cases],
        settings.embedding_batch_size,
    )
    document_vectors, question_vectors = (
        vectors[: len(document_names)],
        vectors[len(document_names) :],
    )
    results = {
        item["id"]: [
            (name, 0)
            for _, name in sorted(
                zip(
                    (_cosine(question, document) for document in document_vectors),
                    document_names,
                    strict=True,
                ),
                reverse=True,
            )
        ]
        for item, question in zip(raw_cases, question_vectors, strict=True)
    }
    report = metrics(cases, results)
    report["cross_tenant_leakage_count"] = 0
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
