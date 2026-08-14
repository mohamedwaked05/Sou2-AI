"""Execute the production Ollama provider against the versioned dataset."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Literal
from uuid import uuid4

from app.agent.owner_chat_provider import (
    OllamaOwnerChatProvider,
    OwnerChatProviderError,
    OwnerChatProviderInvalidResponse,
    OwnerChatProviderTimeout,
    OwnerChatProviderUnavailable,
    OwnerChatResult,
)

from experiments.owner_chat_language_eval.checks import run_deterministic_checks
from experiments.owner_chat_language_eval.dataset import (
    build_provider_request,
    dataset_fingerprint,
    load_dataset,
)
from experiments.owner_chat_language_eval.models import (
    DATASET_VERSION,
    FORMAT_VERSION,
    BusinessFixture,
    EvaluationScenario,
)

PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_BASELINE_PATH = PACKAGE_ROOT / "results" / "baseline.json"
DEFAULT_RERUN_DIRECTORY = PACKAGE_ROOT / "results" / "reruns"
INCOMPLETE_DIRECTORY = PACKAGE_ROOT / "artifacts" / "incomplete"
INCOMPLETE_ARTIFACT_REFERENCE = (
    "experiments/owner_chat_language_eval/artifacts/incomplete"
)
MODEL_CONTRACT_FAILURE = "provider_invalid_response"
BASELINE_COMPLETE_STATUSES = {"complete", "complete_with_model_failures"}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp_slug(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%S%fZ")


def _safe_error_code(error: Exception) -> str:
    if isinstance(error, OwnerChatProviderTimeout):
        return "provider_timeout"
    if isinstance(error, OwnerChatProviderUnavailable):
        return "provider_unavailable"
    if isinstance(error, OwnerChatProviderInvalidResponse):
        return "provider_invalid_response"
    if isinstance(error, OwnerChatProviderError):
        return "provider_error"
    return "unexpected_execution_error"


def _validate_result_matrix(
    document: dict[str, object],
) -> tuple[list[dict[str, object]], int, int]:
    """Validate the fixed evaluation matrix and classify safe result outcomes."""
    if document.get("run_kind") != "baseline":
        raise ValueError("Baseline evidence must have run_kind baseline.")
    if any(
        not isinstance(document.get(field), str) or not document[field].strip()
        for field in ("started_at", "completed_at")
    ):
        raise ValueError("Baseline execution timestamps are invalid.")
    configuration = document.get("configuration")
    if (
        not isinstance(configuration, dict)
        or configuration.get("provider") != "ollama"
        or not isinstance(configuration.get("model"), str)
        or not configuration["model"].strip()
        or configuration.get("scenario_count") != 50
        or configuration.get("attempts_per_scenario") != 1
    ):
        raise ValueError("Baseline configuration is invalid.")
    if (
        document.get("dataset_version") != DATASET_VERSION
        or document.get("dataset_fingerprint_sha256") != dataset_fingerprint()
    ):
        raise ValueError("Baseline does not match the current versioned dataset.")
    raw_results = document.get("results")
    if not isinstance(raw_results, list) or len(raw_results) != 50:
        raise ValueError("Baseline must contain exactly 50 scenario attempts.")
    results: list[dict[str, object]] = []
    valid_count = 0
    invalid_count = 0
    for raw_result in raw_results:
        if not isinstance(raw_result, dict):
            raise ValueError("Baseline results must be JSON objects.")
        error = raw_result.get("execution_error")
        if error is None:
            response = raw_result.get("response")
            if not isinstance(response, str) or not response.strip():
                raise ValueError(
                    "Successful baseline results require a visible response."
                )
            valid_count += 1
        elif error == MODEL_CONTRACT_FAILURE:
            if raw_result.get("response") is not None:
                raise ValueError("Invalid-response records must not expose a response.")
            invalid_count += 1
        else:
            raise ValueError("Baseline contains an infrastructure or execution error.")
        if (
            not isinstance(raw_result.get("scenario_id"), str)
            or not isinstance(raw_result.get("language"), str)
            or not isinstance(raw_result.get("scenario_type"), str)
            or not isinstance(raw_result.get("provider_identifier"), str)
            or not isinstance(raw_result.get("model_identifier"), str)
            or not isinstance(raw_result.get("duration_ms"), (int, float))
            or not isinstance(raw_result.get("started_at"), str)
            or not raw_result["started_at"].strip()
        ):
            raise ValueError("Baseline results are missing required safe metadata.")
        results.append(raw_result)
    expected_matrix = [
        (scenario.id, scenario.language.value, scenario.scenario_type.value)
        for scenario in load_dataset()
    ]
    actual_matrix = [
        (result["scenario_id"], result["language"], result["scenario_type"])
        for result in results
    ]
    if actual_matrix != expected_matrix:
        raise ValueError("Baseline results do not match the versioned scenario matrix.")
    return results, valid_count, invalid_count


def eligible_baseline_attempts(
    document: dict[str, object],
) -> tuple[int, int]:
    """Return valid and model-contract-failure counts for eligible baseline input."""
    _, valid_count, invalid_count = _validate_result_matrix(document)
    return valid_count, invalid_count


def validate_canonical_baseline(
    document: dict[str, object],
) -> tuple[list[dict[str, object]], int, int]:
    """Validate canonical evidence, including its explicit completion status."""
    status = document.get("status")
    if status not in BASELINE_COMPLETE_STATUSES:
        raise ValueError("Manual scoring requires a completed baseline run.")
    results, valid_count, invalid_count = _validate_result_matrix(document)
    expected_status = (
        "complete" if invalid_count == 0 else "complete_with_model_failures"
    )
    if status != expected_status:
        raise ValueError("Baseline completion status does not match its results.")
    return results, valid_count, invalid_count


def _serialize_result(result: OwnerChatResult) -> dict[str, object]:
    usage = result.usage
    return {
        "response": result.reply,
        "proposed_knowledge": [
            {
                "subject_key": fact.subject_key,
                "content": fact.content,
                "kind": fact.kind,
                "category": fact.category,
                "expires_at": fact.expires_at.isoformat()
                if fact.expires_at is not None
                else None,
            }
            for fact in result.proposed_knowledge
        ],
        "usage": None
        if usage is None
        else {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
            "authoritative": usage.authoritative,
        },
        "provider_identifier": result.provider_identifier,
        "model_identifier": result.model_identifier,
    }


def execute_evaluation(
    *,
    provider: OllamaOwnerChatProvider,
    scenarios: Sequence[EvaluationScenario],
    fixture: BusinessFixture,
    run_kind: Literal["baseline", "selective_rerun"],
) -> dict[str, object]:
    """Run each selected scenario once and return a reviewable run document."""
    started_at = _utc_now()
    results: list[dict[str, object]] = []
    interrupted = False

    for scenario in scenarios:
        request = build_provider_request(scenario, fixture)
        scenario_started = _utc_now()
        started_clock = perf_counter()
        record: dict[str, object] = {
            "scenario_id": scenario.id,
            "language": scenario.language.value,
            "scenario_type": scenario.scenario_type.value,
            "started_at": scenario_started.isoformat(),
        }
        try:
            result = provider.generate(request)
            warnings, critical_candidates = run_deterministic_checks(
                scenario, result, fixture.requested_at
            )
            record.update(_serialize_result(result))
            record["deterministic_warnings"] = warnings
            record["critical_failure_candidates"] = critical_candidates
            record["execution_error"] = None
        except KeyboardInterrupt:
            interrupted = True
            record["execution_error"] = "interrupted"
            record["deterministic_warnings"] = []
            record["critical_failure_candidates"] = []
            record["response"] = None
            record["proposed_knowledge"] = []
            record["usage"] = None
            record["provider_identifier"] = "ollama"
            record["model_identifier"] = provider.model
        except Exception as error:  # safe artifact contains only a classified code
            record["execution_error"] = _safe_error_code(error)
            record["deterministic_warnings"] = []
            record["critical_failure_candidates"] = []
            record["response"] = None
            record["proposed_knowledge"] = []
            record["usage"] = None
            record["provider_identifier"] = "ollama"
            record["model_identifier"] = provider.model
        finally:
            record["duration_ms"] = round((perf_counter() - started_clock) * 1_000, 3)
            results.append(record)
        if interrupted:
            break

    expected_count = 50 if run_kind == "baseline" else len(scenarios)
    all_attempts_finished = len(results) == expected_count and not interrupted
    all_successful = all(result["execution_error"] is None for result in results)
    only_model_contract_failures = all(
        result["execution_error"] in {None, MODEL_CONTRACT_FAILURE}
        for result in results
    )
    if (
        run_kind == "baseline"
        and all_attempts_finished
        and only_model_contract_failures
    ):
        status = "complete" if all_successful else "complete_with_model_failures"
    elif run_kind == "selective_rerun" and all_attempts_finished and all_successful:
        status = "complete"
    else:
        status = "incomplete"
    completed_at = _utc_now()
    return {
        "format_version": FORMAT_VERSION,
        "dataset_version": DATASET_VERSION,
        "dataset_fingerprint_sha256": dataset_fingerprint(),
        "run_kind": run_kind,
        "status": status,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "configuration": {
            "provider": "ollama",
            "model": provider.model,
            "timeout_seconds": provider.timeout_seconds,
            "max_output_tokens": fixture.max_output_tokens,
            "fixture_version": fixture.fixture_version,
            "scenario_count": len(scenarios),
            "attempts_per_scenario": 1,
        },
        "results": results,
    }


def write_text_exclusive(path: Path, content: str, *, artifact_name: str) -> Path:
    """Publish a complete artifact atomically without replacing another file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary_path.open("x", encoding="utf-8", newline="\n") as output:
            output.write(content)
        try:
            os.link(temporary_path, path)
        except FileExistsError as exc:
            raise ValueError(
                f"Refusing to overwrite existing {artifact_name}: {path}"
            ) from exc
    finally:
        temporary_path.unlink(missing_ok=True)
    return path


def write_json_exclusive(path: Path, payload: dict[str, object]) -> Path:
    """Write formatted JSON without ever replacing an existing artifact."""
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return write_text_exclusive(path, content, artifact_name="result")


def persist_run_document(
    document: dict[str, object],
    *,
    requested_path: Path | None = None,
    canonical_baseline_path: Path = DEFAULT_BASELINE_PATH,
) -> Path:
    """Keep incomplete work separate and preserve completed baselines."""
    now = _utc_now()
    run_kind = document.get("run_kind")
    if run_kind not in {"baseline", "selective_rerun"}:
        raise ValueError("Evaluation artifact has an invalid run kind.")
    if run_kind == "baseline" and requested_path is not None:
        raise ValueError("A full baseline cannot use a custom output path.")
    if (
        run_kind == "selective_rerun"
        and requested_path is not None
        and requested_path.resolve() == canonical_baseline_path.resolve()
    ):
        raise ValueError("A selective rerun cannot replace the canonical baseline.")

    if document.get("status") not in BASELINE_COMPLETE_STATUSES:
        target = INCOMPLETE_DIRECTORY / f"incomplete-{_timestamp_slug(now)}.json"
    elif run_kind == "baseline":
        target = canonical_baseline_path
    else:
        target = requested_path or (
            DEFAULT_RERUN_DIRECTORY / f"rerun-{_timestamp_slug(now)}.json"
        )
    return write_json_exclusive(target, document)


def _safe_incomplete_artifact_path(reference: Path) -> tuple[Path, str]:
    """Resolve one repository-relative incomplete artifact without path escape."""
    if reference.is_absolute() or ".." in reference.parts:
        raise ValueError("Promotion input must be a relative incomplete-artifact path.")
    repository_root = PACKAGE_ROOT.parents[1]
    candidate = repository_root / reference
    if candidate.is_symlink():
        raise ValueError("Promotion input cannot be a symlink.")
    requested = candidate.resolve(strict=True)
    allowed_directory = INCOMPLETE_DIRECTORY.resolve(strict=True)
    try:
        relative_name = requested.relative_to(allowed_directory)
    except ValueError as exc:
        raise ValueError("Promotion input is outside artifacts/incomplete.") from exc
    if requested.is_dir() or not relative_name.parts or len(relative_name.parts) != 1:
        raise ValueError(
            "Promotion input must be one artifact file in artifacts/incomplete."
        )
    source_reference = f"{INCOMPLETE_ARTIFACT_REFERENCE}/{relative_name.as_posix()}"
    return requested, source_reference


def promote_incomplete_artifact(
    reference: Path,
    *,
    canonical_baseline_path: Path = DEFAULT_BASELINE_PATH,
    promoted_at: datetime | None = None,
) -> Path:
    """Promote one eligible, unbiased incomplete artifact without provider I/O."""
    source_path, source_reference = _safe_incomplete_artifact_path(reference)
    source_bytes = source_path.read_bytes()
    try:
        source_document = json.loads(source_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Promotion input is not valid UTF-8 JSON.") from exc
    if not isinstance(source_document, dict):
        raise ValueError("Promotion input must be a JSON object.")
    valid_count, invalid_count = eligible_baseline_attempts(source_document)
    if source_document.get("status") != "incomplete":
        raise ValueError("Promotion input must be an incomplete artifact.")
    promoted_document = dict(source_document)
    promoted_document["status"] = (
        "complete" if invalid_count == 0 else "complete_with_model_failures"
    )
    promotion_time = promoted_at or _utc_now()
    promoted_document["promotion"] = {
        "source_reference": source_reference,
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "promoted_at": promotion_time.isoformat(),
        "valid_structured_response_count": valid_count,
        "invalid_model_response_count": invalid_count,
    }
    if canonical_baseline_path.exists():
        raise ValueError(
            f"Canonical baseline already exists and will not be replaced: "
            f"{canonical_baseline_path}"
        )
    return write_json_exclusive(canonical_baseline_path, promoted_document)
