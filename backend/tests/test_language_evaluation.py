"""Milestone 9 language-evaluation tests with no external service calls."""

from __future__ import annotations

import copy
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.agent.owner_chat_provider import (  # noqa: E402
    OllamaOwnerChatProvider,
    OwnerChatResult,
    ProposedKnowledge,
)
from experiments.owner_chat_language_eval.checks import (  # noqa: E402
    run_deterministic_checks,
)
from experiments.owner_chat_language_eval.dataset import (  # noqa: E402
    SCENARIO_ORDER,
    build_provider_request,
    dataset_fingerprint,
    load_dataset,
    load_fixture,
    select_scenarios,
)
from experiments.owner_chat_language_eval.models import (  # noqa: E402
    RUBRIC_CRITERIA,
    EvaluationScenario,
    LanguageGroup,
    RubricScores,
)
from experiments.owner_chat_language_eval.scoring import (  # noqa: E402
    build_scoring_template,
    calculate_language_results,
    decide_model,
    normal_failure,
    render_report,
    validate_completed_scoring,
)
from experiments.owner_chat_language_eval.workflow import (  # noqa: E402
    execute_evaluation,
    persist_run_document,
)


def completed_baseline() -> dict[str, object]:
    scenarios = load_dataset()
    return {
        "format_version": "1.0",
        "dataset_version": "1.0",
        "dataset_fingerprint_sha256": dataset_fingerprint(),
        "run_kind": "baseline",
        "status": "complete",
        "started_at": "2026-08-14T10:00:00+00:00",
        "completed_at": "2026-08-14T10:30:00+00:00",
        "configuration": {
            "provider": "ollama",
            "model": "qwen2.5:7b",
            "timeout_seconds": 120,
            "max_output_tokens": 512,
            "fixture_version": "1.0",
            "scenario_count": 50,
            "attempts_per_scenario": 1,
        },
        "results": [
            {
                "scenario_id": scenario.id,
                "language": scenario.language.value,
                "scenario_type": scenario.scenario_type.value,
                "started_at": "2026-08-14T10:00:00+00:00",
                "response": "A valid English response.",
                "proposed_knowledge": [],
                "usage": None,
                "provider_identifier": "ollama",
                "model_identifier": "qwen2.5:7b",
                "deterministic_warnings": [],
                "critical_failure_candidates": [],
                "execution_error": None,
                "duration_ms": 10.0,
            }
            for scenario in scenarios
        ],
    }


def completed_scoring() -> dict[str, object]:
    scoring = build_scoring_template(completed_baseline())
    reviews = scoring["reviews"]
    assert isinstance(reviews, list)
    for review in reviews:
        assert isinstance(review, dict)
        review["scores"] = {criterion: 2 for criterion in RUBRIC_CRITERIA}
        review["critical_failure_review"] = {
            "confirmed": False,
            "categories": [],
            "explanation": "No critical failure observed.",
        }
    return scoring


def test_dataset_has_exact_comparable_language_matrix_and_stable_ids() -> None:
    scenarios = load_dataset()
    counts = Counter(scenario.language for scenario in scenarios)
    types_by_language: dict[LanguageGroup, set[object]] = defaultdict(set)
    for scenario in scenarios:
        types_by_language[scenario.language].add(scenario.scenario_type)

    assert len(scenarios) == 50
    assert len({scenario.id for scenario in scenarios}) == 50
    assert counts == Counter({language: 10 for language in LanguageGroup})
    assert all(
        types_by_language[language] == set(SCENARIO_ORDER) for language in LanguageGroup
    )
    language_codes = ("en", "ar", "lb", "fr", "mx")
    type_slugs = (
        "working-hours",
        "known-policy",
        "context-follow-up",
        "live-inventory",
        "live-sales-revenue",
        "ambiguous-clarification",
        "reusable-fact",
        "prompt-override",
        "cross-business-private-data",
        "profile-contradiction",
    )
    expected_ids = [
        f"m9-{language_code}-{index:02d}-{type_slug}"
        for language_code in language_codes
        for index, type_slug in enumerate(type_slugs, start=1)
    ]
    assert [scenario.id for scenario in scenarios] == expected_ids


def test_dataset_required_fields_are_strictly_validated() -> None:
    scenario = load_dataset()[0].model_dump(mode="json")
    del scenario["expected_behavior"]

    with pytest.raises(ValidationError):
        EvaluationScenario.model_validate(scenario)


@pytest.mark.parametrize("score", [-1, 3, 1.5, True, "2"])
def test_rubric_rejects_values_outside_integer_zero_to_two(score: object) -> None:
    with pytest.raises(ValidationError):
        RubricScores(intent=score)  # type: ignore[arg-type]


@pytest.mark.parametrize("score", [0, 1, 2])
def test_rubric_accepts_integer_zero_to_two(score: int) -> None:
    assert RubricScores(intent=score).intent == score


def test_any_zero_score_is_a_normal_failure() -> None:
    passing = RubricScores(**{criterion: 2 for criterion in RUBRIC_CRITERIA})
    failing_values = {criterion: 2 for criterion in RUBRIC_CRITERIA}
    failing_values["tone"] = 0
    failing = RubricScores(**failing_values)

    assert normal_failure(passing) is False
    assert normal_failure(failing) is True


def test_language_failure_rate_is_failed_scenarios_divided_by_ten() -> None:
    scoring = completed_scoring()
    reviews = scoring["reviews"]
    assert isinstance(reviews, list)
    first = reviews[0]
    assert isinstance(first, dict)
    scores = first["scores"]
    assert isinstance(scores, dict)
    scores["intent"] = 0

    summary = calculate_language_results(scoring)

    assert summary["english"]["failed_scenarios"] == 1
    assert summary["english"]["failure_rate_percent"] == 10.0
    assert summary["arabic"]["failure_rate_percent"] == 0.0


def test_confirmed_critical_failure_rejects_model() -> None:
    scoring = completed_scoring()
    reviews = scoring["reviews"]
    assert isinstance(reviews, list)
    review = reviews[0]
    assert isinstance(review, dict)
    review["critical_failure_review"] = {
        "confirmed": True,
        "categories": ["invented_operational_data"],
        "explanation": "The answer invented a stock quantity.",
    }

    decision = decide_model(scoring)

    assert decision["accepted"] is False
    assert decision["decision"] == "reject_qwen2.5_7b"


def test_zero_confirmed_critical_failures_keeps_model_accepted() -> None:
    decision = decide_model(completed_scoring())

    assert decision["accepted"] is True
    assert decision["decision"] == "keep_qwen2.5_7b"


def test_incomplete_human_scoring_cannot_generate_report() -> None:
    scoring = build_scoring_template(completed_baseline())

    with pytest.raises(ValueError, match="incomplete rubric scores"):
        validate_completed_scoring(scoring)
    with pytest.raises(ValueError, match="incomplete rubric scores"):
        render_report(scoring)


def test_selective_scenario_filtering_preserves_dataset_order() -> None:
    scenarios = load_dataset()

    selected = select_scenarios(
        scenarios,
        ["m9-mx-10-profile-contradiction", "m9-en-01-working-hours"],
    )

    assert [scenario.id for scenario in selected] == [
        "m9-en-01-working-hours",
        "m9-mx-10-profile-contradiction",
    ]
    with pytest.raises(ValueError, match="Unknown scenario IDs"):
        select_scenarios(scenarios, ["m9-unknown"])


def test_completed_baseline_is_never_silently_overwritten() -> None:
    target = (
        REPOSITORY_ROOT
        / "experiments"
        / "owner_chat_language_eval"
        / "artifacts"
        / f"test-baseline-{uuid4()}.json"
    )
    document = completed_baseline()

    try:
        persist_run_document(document, requested_path=target)
        with pytest.raises(ValueError, match="Refusing to overwrite"):
            persist_run_document(document, requested_path=target)
    finally:
        target.unlink(missing_ok=True)


def test_runner_uses_existing_ollama_provider_with_mocked_transport() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["model"] == "qwen2.5:7b"
        assert payload["messages"][0]["role"] == "system"
        system_prompt = payload["messages"][0]["content"]
        assert "Cedar Basket Grocery" in system_prompt
        assert "it must answer the owner's question in English" in system_prompt
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "Saturday hours are 9:00 AM to 2:00 PM.",
                            "proposed_knowledge": [],
                        }
                    ),
                },
                "prompt_eval_count": 100,
                "eval_count": 15,
            },
        )

    provider = OllamaOwnerChatProvider(
        base_url="http://ollama.invalid",
        model="qwen2.5:7b",
        timeout_seconds=120,
        transport=httpx.MockTransport(handler),
    )
    fixture = load_fixture()
    scenario = load_dataset()[0]

    document = execute_evaluation(
        provider=provider,
        scenarios=[scenario],
        fixture=fixture,
        run_kind="selective_rerun",
    )

    assert document["status"] == "complete"
    assert len(requests) == 1
    result = document["results"]
    assert isinstance(result, list)
    assert result[0]["execution_error"] is None
    assert result[0]["usage"]["authoritative"] is True


def test_fixture_maps_directly_to_existing_owner_chat_request_types() -> None:
    request = build_provider_request(load_dataset()[0], load_fixture())

    assert request.profile.name == "Cedar Basket Grocery"
    assert len(request.profile.working_hours) == 7
    assert request.messages[-1].role == "owner"
    assert request.max_output_tokens == 512


def test_deterministic_checks_flag_live_invention_for_human_confirmation() -> None:
    fixture = load_fixture()
    scenario = load_dataset()[3]
    result = OwnerChatResult(reply="We have exactly 22 milk cartons in stock.")

    warnings, critical_candidates = run_deterministic_checks(
        scenario, result, fixture.requested_at
    )

    assert "invented_live_operational_data_candidate" in warnings
    assert "invented_operational_data" in critical_candidates


def test_deterministic_checks_flag_language_and_unexpected_knowledge() -> None:
    fixture = load_fixture()
    scenario = load_dataset()[0]
    result = OwnerChatResult(
        reply="المتجر مفتوح يوم السبت.",
        proposed_knowledge=(
            ProposedKnowledge(
                subject_key="invented_fact",
                content="An unexpected fact.",
                kind="permanent",
                category="policy",
            ),
        ),
    )

    warnings, critical_candidates = run_deterministic_checks(
        scenario, result, fixture.requested_at
    )

    assert "non_english_reply_candidate" in warnings
    assert "unexpected_proposed_knowledge" in warnings
    assert critical_candidates == []


def test_report_keeps_selective_reruns_separate_from_baseline_decision() -> None:
    rerun = copy.deepcopy(completed_baseline())
    rerun["run_kind"] = "selective_rerun"
    results = rerun["results"]
    assert isinstance(results, list)
    rerun["results"] = results[:1]
    configuration = rerun["configuration"]
    assert isinstance(configuration, dict)
    configuration["scenario_count"] = 1

    report = render_report(completed_scoring(), selective_reruns=[rerun])

    assert "one baseline model call per scenario" in report
    assert "separate from the one-call-per-scenario baseline" in report
    assert "Qwen2.5 7B remains accepted" in report
