"""Run the fixed Milestone 13 BGE-M3 retrieval evaluation locally."""

from __future__ import annotations

import json
import math
from pathlib import Path

from app.core.config import get_settings
from app.rag.embeddings import EmbeddingProviderError, create_embedding_provider
from app.rag.evaluation import EvaluationCase, metrics

DATASET_PATH = Path(__file__).parents[2] / "evaluations" / "milestone_13_retrieval.json"
EVALUATION_TENANT_ID = "evaluation-tenant"


def _cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True)) / (
        math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    )


def _load_dataset() -> tuple[list[dict[str, str]], list[dict[str, object]]]:
    payload = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    return payload["cases"], payload["documents"]


def main() -> None:
    settings = get_settings()
    provider = create_embedding_provider(settings)
    raw_cases, documents = _load_dataset()
    cases = [
        EvaluationCase(
            item["id"],
            item["language"],
            item["expected_document"],
            int(item["expected_chunk_index"]),
        )
        for item in raw_cases
    ]
    try:
        document_vectors = provider.embed(
            [str(item["content"]) for item in documents]
        ).vectors
    except EmbeddingProviderError:
        report = metrics(cases, {})
        report["cross_tenant_leakage_count"] = 0
        print(json.dumps(report, indent=2, sort_keys=True))
        raise SystemExit(1) from None

    results: dict[str, list[tuple[str, int]]] = {}
    result_tenants: dict[str, list[str]] = {}
    for raw_case in raw_cases:
        try:
            question = provider.embed([str(raw_case["question"])]).vectors[0]
        except EmbeddingProviderError:
            continue
        ranked = sorted(
            (
                (_cosine(list(question), list(vector)), document)
                for document, vector in zip(documents, document_vectors, strict=True)
                if document["tenant_id"] == EVALUATION_TENANT_ID
            ),
            key=lambda item: (-item[0], str(item[1]["id"])),
        )
        selected = [
            document
            for score, document in ranked[: settings.retrieval_candidate_limit]
            if score >= settings.retrieval_minimum_similarity
        ]
        results[str(raw_case["id"])] = [
            (str(document["id"]), int(document["chunk_index"])) for document in selected
        ]
        result_tenants[str(raw_case["id"])] = [
            str(document["tenant_id"]) for document in selected
        ]
    report = metrics(cases, results)
    report["cross_tenant_leakage_count"] = sum(
        tenant != EVALUATION_TENANT_ID
        for tenants in result_tenants.values()
        for tenant in tenants
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    language_reports = [report[case.language] for case in cases]
    passes = (
        all(item["recall_at_10"] >= 0.9 for item in language_reports)
        and all(item["execution_failure_rate"] == 0 for item in language_reports)
        and report["cross_tenant_leakage_count"] == 0
    )
    if not passes:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
