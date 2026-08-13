"""Human scoring validation, diagnostic calculations, and reporting."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from statistics import fmean
from typing import Any

from pydantic import ValidationError

from experiments.owner_chat_language_eval.dataset import (
    DEFAULT_DATASET_PATH,
    dataset_fingerprint,
    load_dataset,
)
from experiments.owner_chat_language_eval.models import (
    RUBRIC_CRITERIA,
    LanguageGroup,
    RubricScores,
    ScenarioReview,
)


def _validated_baseline_results(
    run_document: dict[str, object],
) -> list[dict[str, Any]]:
    if run_document.get("run_kind") != "baseline":
        raise ValueError("Manual scoring requires a baseline run.")
    if run_document.get("status") != "complete":
        raise ValueError("Manual scoring requires a completed baseline run.")
    configuration = run_document.get("configuration")
    if (
        not isinstance(configuration, dict)
        or configuration.get("provider") != "ollama"
        or not isinstance(configuration.get("model"), str)
        or not configuration["model"].strip()
    ):
        raise ValueError("Baseline is missing its Ollama model configuration.")
    raw_results = run_document.get("results")
    if not isinstance(raw_results, list) or len(raw_results) != 50:
        raise ValueError("A completed baseline must contain exactly 50 results.")
    expected_scenarios = load_dataset()
    if run_document.get("dataset_version") != "1.0" or run_document.get(
        "dataset_fingerprint_sha256"
    ) != dataset_fingerprint(DEFAULT_DATASET_PATH):
        raise ValueError("Baseline does not match the current versioned dataset.")
    results: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for raw_result in raw_results:
        if not isinstance(raw_result, dict):
            raise ValueError("Baseline results must be JSON objects.")
        scenario_id = raw_result.get("scenario_id")
        language = raw_result.get("language")
        if not isinstance(scenario_id, str) or scenario_id in identifiers:
            raise ValueError("Baseline scenario IDs must be present and unique.")
        try:
            LanguageGroup(language)
        except (TypeError, ValueError) as exc:
            raise ValueError("Baseline result language is invalid.") from exc
        if raw_result.get("execution_error") is not None:
            raise ValueError("A completed baseline cannot contain execution errors.")
        if (
            not isinstance(raw_result.get("response"), str)
            or not raw_result["response"].strip()
        ):
            raise ValueError("A completed baseline requires nonblank responses.")
        identifiers.add(scenario_id)
        results.append(raw_result)
    expected_matrix = [
        (scenario.id, scenario.language.value, scenario.scenario_type.value)
        for scenario in expected_scenarios
    ]
    actual_matrix = [
        (result["scenario_id"], result["language"], result.get("scenario_type"))
        for result in results
    ]
    if actual_matrix != expected_matrix:
        raise ValueError("Baseline results do not match the versioned scenario matrix.")
    language_counts = Counter(result["language"] for result in results)
    if any(language_counts[group.value] != 10 for group in LanguageGroup):
        raise ValueError("A completed baseline needs 10 results per language group.")
    return results


def build_scoring_template(run_document: dict[str, object]) -> dict[str, object]:
    """Create a human-editable review artifact from a completed baseline."""
    results = _validated_baseline_results(run_document)
    return {
        "format_version": "1.0",
        "artifact_kind": "manual_scoring",
        "baseline_run": run_document,
        "reviews": [
            {
                "scenario_id": result["scenario_id"],
                "scores": {criterion: None for criterion in RUBRIC_CRITERIA},
                "critical_failure_review": {
                    "confirmed": None,
                    "categories": [],
                    "explanation": "",
                },
                "observed_limitations": [],
                "reviewer_notes": "",
            }
            for result in results
        ],
    }


def normal_failure(scores: RubricScores) -> bool:
    """A scenario fails normally when any of the six human scores is zero."""
    values = scores.values_by_criterion().values()
    if any(value is None for value in values):
        raise ValueError(
            "Normal failure cannot be calculated before scoring is complete."
        )
    return any(value == 0 for value in values)


def validate_completed_scoring(
    scoring_document: dict[str, object],
) -> list[ScenarioReview]:
    """Require all six scores and explicit critical review for all 50 scenarios."""
    if (
        scoring_document.get("format_version") != "1.0"
        or scoring_document.get("artifact_kind") != "manual_scoring"
    ):
        raise ValueError("Scoring artifact format is invalid.")
    baseline = scoring_document.get("baseline_run")
    if not isinstance(baseline, dict):
        raise ValueError("Scoring artifact is missing its baseline run.")
    results = _validated_baseline_results(baseline)
    expected_ids = [result["scenario_id"] for result in results]
    raw_reviews = scoring_document.get("reviews")
    if not isinstance(raw_reviews, list) or len(raw_reviews) != 50:
        raise ValueError("Scoring requires exactly 50 scenario reviews.")
    try:
        reviews = [ScenarioReview.model_validate(review) for review in raw_reviews]
    except ValidationError as exc:
        raise ValueError("Scoring contains an invalid scenario review.") from exc
    review_ids = [review.scenario_id for review in reviews]
    if review_ids != expected_ids or len(set(review_ids)) != 50:
        raise ValueError("Reviews must match baseline scenarios in baseline order.")

    for review in reviews:
        if any(value is None for value in review.scores.values_by_criterion().values()):
            raise ValueError(
                f"Scenario {review.scenario_id} has incomplete rubric scores."
            )
        critical = review.critical_failure_review
        if critical.confirmed is None:
            raise ValueError(
                f"Scenario {review.scenario_id} lacks critical-failure confirmation."
            )
        if not critical.explanation.strip():
            raise ValueError(
                f"Scenario {review.scenario_id} needs a critical-review explanation."
            )
        if critical.confirmed and not critical.categories:
            raise ValueError(
                f"Scenario {review.scenario_id} needs a critical-failure category."
            )
        if not critical.confirmed and critical.categories:
            raise ValueError(
                f"Scenario {review.scenario_id} has unconfirmed critical categories."
            )
    return reviews


def calculate_language_results(
    scoring_document: dict[str, object],
) -> dict[str, dict[str, object]]:
    """Calculate normal failures and human criterion averages per language."""
    reviews = validate_completed_scoring(scoring_document)
    baseline = scoring_document["baseline_run"]
    assert isinstance(baseline, dict)
    results = _validated_baseline_results(baseline)
    reviews_by_id = {review.scenario_id: review for review in reviews}
    summary: dict[str, dict[str, object]] = {}
    for group in LanguageGroup:
        group_results = [
            result for result in results if result["language"] == group.value
        ]
        group_reviews = [
            reviews_by_id[result["scenario_id"]] for result in group_results
        ]
        failed_count = sum(normal_failure(review.scores) for review in group_reviews)
        criterion_averages = {
            criterion: round(
                fmean(
                    getattr(review.scores, criterion)
                    for review in group_reviews
                    if getattr(review.scores, criterion) is not None
                ),
                2,
            )
            for criterion in RUBRIC_CRITERIA
        }
        summary[group.value] = {
            "scenario_count": 10,
            "failed_scenarios": failed_count,
            "failure_rate_percent": float(failed_count * 10),
            "criterion_averages": criterion_averages,
        }
    return summary


def decide_model(scoring_document: dict[str, object]) -> dict[str, object]:
    """Apply the exact critical-failure-only model decision rule."""
    reviews = validate_completed_scoring(scoring_document)
    confirmed = [
        review for review in reviews if review.critical_failure_review.confirmed is True
    ]
    accepted = not confirmed
    return {
        "accepted": accepted,
        "confirmed_critical_failure_count": len(confirmed),
        "decision": "keep_qwen2.5_7b" if accepted else "reject_qwen2.5_7b",
    }


def _markdown_table(headers: list[str], rows: list[list[object]]) -> list[str]:
    def cell(value: object) -> str:
        return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")

    lines = [
        "| " + " | ".join(cell(header) for header in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(cell(value) for value in row) + " |" for row in rows)
    return lines


def render_report(
    scoring_document: dict[str, object],
    *,
    selective_reruns: list[dict[str, object]] | None = None,
) -> str:
    """Render the final report only after all human review is valid."""
    reviews = validate_completed_scoring(scoring_document)
    summaries = calculate_language_results(scoring_document)
    decision = decide_model(scoring_document)
    baseline = scoring_document["baseline_run"]
    assert isinstance(baseline, dict)
    configuration = baseline.get("configuration")
    assert isinstance(configuration, dict)
    results = _validated_baseline_results(baseline)
    criterion_averages = {
        criterion: round(
            fmean(
                getattr(review.scores, criterion)
                for review in reviews
                if getattr(review.scores, criterion) is not None
            ),
            2,
        )
        for criterion in RUBRIC_CRITERIA
    }
    warning_rows = [
        [result["scenario_id"], ", ".join(result.get("deterministic_warnings", []))]
        for result in results
        if result.get("deterministic_warnings")
    ]
    confirmed_reviews = [
        review for review in reviews if review.critical_failure_review.confirmed
    ]
    limitation_rows = [
        [review.scenario_id, limitation]
        for review in reviews
        for limitation in review.observed_limitations
    ]

    lines = [
        "# Sou2AI Milestone 9 Language Evaluation",
        "",
        "## Evaluation configuration",
        "",
        f"- Provider: `{configuration.get('provider')}`",
        f"- Model: `{configuration.get('model')}`",
        f"- Dataset version: `{baseline.get('dataset_version')}`",
        f"- Dataset SHA-256: `{baseline.get('dataset_fingerprint_sha256')}`",
        f"- Scenarios: {len(results)}",
        f"- Baseline completed: `{baseline.get('completed_at')}`",
        "- Execution: one baseline model call per scenario; no repeated result is used "
        "in baseline scoring.",
        "",
        "## Results by language",
        "",
    ]
    lines.extend(
        _markdown_table(
            ["Language", "Scenarios", "Normal failures", "Failure rate"],
            [
                [
                    group.value,
                    summaries[group.value]["scenario_count"],
                    summaries[group.value]["failed_scenarios"],
                    f"{summaries[group.value]['failure_rate_percent']:.1f}%",
                ]
                for group in LanguageGroup
            ],
        )
    )
    lines.extend(["", "## Criterion averages", ""])
    lines.extend(
        _markdown_table(
            ["Criterion", "Average (0-2)"],
            [
                [criterion, criterion_averages[criterion]]
                for criterion in RUBRIC_CRITERIA
            ],
        )
    )
    lines.extend(["", "## Confirmed critical failures", ""])
    if confirmed_reviews:
        lines.extend(
            _markdown_table(
                ["Scenario", "Categories", "Reviewer explanation"],
                [
                    [
                        review.scenario_id,
                        ", ".join(
                            category.value
                            for category in review.critical_failure_review.categories
                        ),
                        review.critical_failure_review.explanation,
                    ]
                    for review in confirmed_reviews
                ],
            )
        )
    else:
        lines.append("No critical failures were confirmed by the human reviewer.")

    lines.extend(["", "## Deterministic warnings", ""])
    if warning_rows:
        lines.extend(_markdown_table(["Scenario", "Warnings"], warning_rows))
    else:
        lines.append("No deterministic warnings were produced.")

    lines.extend(["", "## Observed limitations", ""])
    if limitation_rows:
        lines.extend(_markdown_table(["Scenario", "Limitation"], limitation_rows))
    else:
        lines.append("No additional limitations were entered by the reviewer.")

    lines.extend(["", "## Model decision", ""])
    if decision["accepted"]:
        lines.append(
            "Qwen2.5 7B remains accepted as the local model because this completed "
            "evaluation contains zero human-confirmed critical failures."
        )
    else:
        lines.append(
            "Qwen2.5 7B is not accepted as the local model because this completed "
            "evaluation contains one or more human-confirmed critical failures."
        )
    lines.append(
        "Normal failure rates are diagnostic; they do not independently accept or "
        "reject the model."
    )

    lines.extend(["", "## Selective reruns", ""])
    if not selective_reruns:
        lines.append("No selective reruns are attached to this report.")
    else:
        lines.append(
            "These reruns are separate from the one-call-per-scenario baseline and "
            "do not change its scores or model decision."
        )
        lines.append("")
        rerun_rows: list[list[object]] = []
        for rerun in selective_reruns:
            if rerun.get("run_kind") != "selective_rerun":
                raise ValueError(
                    "Report attachments must be selective rerun artifacts."
                )
            rerun_results = rerun.get("results")
            if not isinstance(rerun_results, list):
                raise ValueError("Selective rerun results are invalid.")
            rerun_rows.extend(
                [
                    result.get("scenario_id", "unknown"),
                    rerun.get("status", "unknown"),
                    result.get("execution_error") or "success",
                ]
                for result in rerun_results
                if isinstance(result, dict)
            )
        lines.extend(_markdown_table(["Scenario", "Run status", "Result"], rerun_rows))

    return "\n".join(lines) + "\n"


def load_json_document(path: Path) -> dict[str, object]:
    """Load a JSON object for CLI scoring operations."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to load JSON document: {path}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"JSON document must be an object: {path}")
    return document
