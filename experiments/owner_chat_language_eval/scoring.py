"""Human scoring validation, diagnostic calculations, and reporting."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from pathlib import Path, PurePosixPath
from statistics import fmean
from typing import Any

from pydantic import ValidationError

from experiments.owner_chat_language_eval.models import (
    RUBRIC_CRITERIA,
    LanguageGroup,
    RubricScores,
    ScenarioReview,
)
from experiments.owner_chat_language_eval.workflow import (
    MODEL_CONTRACT_FAILURE,
    validate_canonical_baseline,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_BASELINE_REFERENCE = (
    "experiments/owner_chat_language_eval/results/baseline.json"
)
SCORING_FORMAT_VERSION = "2.0"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def _canonical_baseline_path(repository_root: Path) -> Path:
    reference = PurePosixPath(CANONICAL_BASELINE_REFERENCE)
    return repository_root.joinpath(*reference.parts)


def _read_canonical_baseline(repository_root: Path) -> bytes:
    path = _canonical_baseline_path(repository_root)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Canonical baseline is unavailable: {path}") from exc


def _parse_json_object(content: bytes, *, artifact_name: str) -> dict[str, object]:
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{artifact_name} is not valid UTF-8 JSON.") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{artifact_name} must be a JSON object.")
    return document


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _validate_baseline_reference(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(
            "Scoring artifact is missing its canonical baseline reference."
        )
    reference = PurePosixPath(value)
    if (
        reference.is_absolute()
        or "\\" in value
        or ".." in reference.parts
        or value != CANONICAL_BASELINE_REFERENCE
    ):
        raise ValueError("Scoring artifact baseline reference is not canonical.")
    return value


def _validated_baseline_results(
    run_document: dict[str, object],
) -> list[dict[str, Any]]:
    results, _, _ = validate_canonical_baseline(run_document)
    return results


def build_scoring_template(
    *, repository_root: Path = REPOSITORY_ROOT
) -> dict[str, object]:
    """Create reviews cryptographically bound to the canonical baseline bytes."""
    baseline_bytes = _read_canonical_baseline(repository_root)
    run_document = _parse_json_object(
        baseline_bytes, artifact_name="Canonical baseline"
    )
    results = _validated_baseline_results(run_document)
    return {
        "format_version": SCORING_FORMAT_VERSION,
        "artifact_kind": "manual_scoring",
        "baseline_reference": CANONICAL_BASELINE_REFERENCE,
        "baseline_sha256": _sha256(baseline_bytes),
        "reviews": [
            {
                "scenario_id": result["scenario_id"],
                "baseline_execution_error": result["execution_error"],
                "deterministic_warnings": list(result.get("deterministic_warnings", []))
                + (
                    [MODEL_CONTRACT_FAILURE]
                    if result["execution_error"] == MODEL_CONTRACT_FAILURE
                    else []
                ),
                "reviewer_guidance": (
                    "No valid visible response was produced. Score all six criteria; "
                    "instruction_following must be 0 because the model failed the "
                    "required structured-response contract. Score the other criteria "
                    "from the absence of a usable answer. This is not automatically a "
                    "critical failure."
                    if result["execution_error"] == MODEL_CONTRACT_FAILURE
                    else ""
                ),
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


def _load_verified_baseline(
    scoring_document: dict[str, object],
    *,
    repository_root: Path,
) -> tuple[dict[str, object], list[dict[str, Any]]]:
    _validate_baseline_reference(scoring_document.get("baseline_reference"))
    expected_fingerprint = scoring_document.get("baseline_sha256")
    if (
        not isinstance(expected_fingerprint, str)
        or SHA256_PATTERN.fullmatch(expected_fingerprint) is None
    ):
        raise ValueError("Scoring artifact baseline fingerprint is invalid.")
    baseline_bytes = _read_canonical_baseline(repository_root)
    actual_fingerprint = _sha256(baseline_bytes)
    if not hmac.compare_digest(expected_fingerprint, actual_fingerprint):
        raise ValueError(
            "Canonical baseline changed after this scoring artifact was prepared."
        )
    baseline = _parse_json_object(baseline_bytes, artifact_name="Canonical baseline")
    return baseline, _validated_baseline_results(baseline)


def normal_failure(
    scores: RubricScores,
    *,
    baseline_execution_error: str | None = None,
) -> bool:
    """A scenario fails normally when any of the six human scores is zero."""
    values = scores.values_by_criterion().values()
    if any(value is None for value in values):
        raise ValueError(
            "Normal failure cannot be calculated before scoring is complete."
        )
    return baseline_execution_error == MODEL_CONTRACT_FAILURE or any(
        value == 0 for value in values
    )


def validate_completed_scoring(
    scoring_document: dict[str, object],
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> list[ScenarioReview]:
    """Require all six scores and explicit critical review for all 50 scenarios."""
    if (
        scoring_document.get("format_version") != SCORING_FORMAT_VERSION
        or scoring_document.get("artifact_kind") != "manual_scoring"
        or "baseline_run" in scoring_document
    ):
        raise ValueError("Scoring artifact format is invalid.")
    _, results = _load_verified_baseline(
        scoring_document, repository_root=repository_root
    )
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

    results_by_id = {result["scenario_id"]: result for result in results}
    for review in reviews:
        baseline_result = results_by_id[review.scenario_id]
        expected_error = baseline_result["execution_error"]
        if review.baseline_execution_error != expected_error:
            raise ValueError(
                f"Scenario {review.scenario_id} does not match its baseline outcome."
            )
        if expected_error == MODEL_CONTRACT_FAILURE:
            if MODEL_CONTRACT_FAILURE not in review.deterministic_warnings:
                raise ValueError(
                    f"Scenario {review.scenario_id} lacks its contract-failure warning."
                )
            if review.scores.instruction_following != 0:
                raise ValueError(
                    f"Scenario {review.scenario_id} must score instruction_following 0."
                )
        elif review.deterministic_warnings != list(
            baseline_result.get("deterministic_warnings", [])
        ):
            raise ValueError(
                f"Scenario {review.scenario_id} does not match baseline warnings."
            )
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
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> dict[str, dict[str, object]]:
    """Calculate normal failures and human criterion averages per language."""
    reviews = validate_completed_scoring(
        scoring_document, repository_root=repository_root
    )
    _, results = _load_verified_baseline(
        scoring_document, repository_root=repository_root
    )
    reviews_by_id = {review.scenario_id: review for review in reviews}
    results_by_id = {result["scenario_id"]: result for result in results}
    summary: dict[str, dict[str, object]] = {}
    for group in LanguageGroup:
        group_results = [
            result for result in results if result["language"] == group.value
        ]
        group_reviews = [
            reviews_by_id[result["scenario_id"]] for result in group_results
        ]
        failed_count = sum(
            normal_failure(
                review.scores,
                baseline_execution_error=results_by_id[review.scenario_id][
                    "execution_error"
                ],
            )
            for review in group_reviews
        )
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


def decide_model(
    scoring_document: dict[str, object],
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> dict[str, object]:
    """Apply the exact critical-failure-only model decision rule."""
    reviews = validate_completed_scoring(
        scoring_document, repository_root=repository_root
    )
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
    repository_root: Path = REPOSITORY_ROOT,
) -> str:
    """Render the final report only after all human review is valid."""
    reviews = validate_completed_scoring(
        scoring_document, repository_root=repository_root
    )
    summaries = calculate_language_results(
        scoring_document, repository_root=repository_root
    )
    decision = decide_model(scoring_document, repository_root=repository_root)
    baseline, results = _load_verified_baseline(
        scoring_document, repository_root=repository_root
    )
    configuration = baseline.get("configuration")
    assert isinstance(configuration, dict)
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
        [
            result["scenario_id"],
            ", ".join(
                list(result.get("deterministic_warnings", []))
                + (
                    [MODEL_CONTRACT_FAILURE]
                    if result["execution_error"] == MODEL_CONTRACT_FAILURE
                    else []
                )
            ),
        ]
        for result in results
        if result.get("deterministic_warnings")
        or result["execution_error"] == MODEL_CONTRACT_FAILURE
    ]
    invalid_response_rows = [
        [result["language"], result["scenario_id"]]
        for result in results
        if result["execution_error"] == MODEL_CONTRACT_FAILURE
    ]
    valid_response_count = sum(result["execution_error"] is None for result in results)
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
        f"- Valid structured responses: {valid_response_count}",
        f"- Invalid model responses: {len(invalid_response_rows)}",
        f"- Baseline started: `{baseline.get('started_at')}`",
        f"- Baseline completed: `{baseline.get('completed_at')}`",
        "- Execution: one baseline model call per scenario; no repeated result is used "
        "in baseline scoring.",
        "",
        "## Invalid structured responses",
        "",
    ]
    if invalid_response_rows:
        lines.append(
            "These are model response-contract failures and always count as normal "
            "failures. They are not automatically human-confirmed critical failures."
        )
        lines.append("")
        lines.extend(_markdown_table(["Language", "Scenario"], invalid_response_rows))
    else:
        lines.append("No invalid structured responses occurred in this baseline.")
    promotion = baseline.get("promotion")
    if isinstance(promotion, dict):
        lines.extend(["", "## Promotion provenance", ""])
        lines.extend(
            [
                f"- Source artifact: `{promotion.get('source_reference')}`",
                f"- Source SHA-256: `{promotion.get('source_sha256')}`",
                f"- Promoted at: `{promotion.get('promoted_at')}`",
                "- Promoted valid structured responses: "
                f"{promotion.get('valid_structured_response_count')}",
                "- Promoted invalid model responses: "
                f"{promotion.get('invalid_model_response_count')}",
            ]
        )
    lines.extend(
        [
            "## Results by language",
            "",
        ]
    )
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
