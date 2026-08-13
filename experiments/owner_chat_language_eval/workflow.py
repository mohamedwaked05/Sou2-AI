"""Execute the production Ollama provider against the versioned dataset."""

from __future__ import annotations

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
    all_successful = (
        len(results) == expected_count
        and all(result["execution_error"] is None for result in results)
        and not interrupted
    )
    completed_at = _utc_now()
    return {
        "format_version": FORMAT_VERSION,
        "dataset_version": DATASET_VERSION,
        "dataset_fingerprint_sha256": dataset_fingerprint(),
        "run_kind": run_kind,
        "status": "complete" if all_successful else "incomplete",
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
) -> Path:
    """Keep incomplete work separate and preserve completed baselines."""
    now = _utc_now()
    if document.get("status") != "complete":
        target = INCOMPLETE_DIRECTORY / f"incomplete-{_timestamp_slug(now)}.json"
    elif document.get("run_kind") == "baseline":
        target = requested_path or DEFAULT_BASELINE_PATH
    else:
        target = requested_path or (
            DEFAULT_RERUN_DIRECTORY / f"rerun-{_timestamp_slug(now)}.json"
        )
    return write_json_exclusive(target, document)
