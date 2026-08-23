"""Regression coverage for safe grounded-evaluation diagnostics."""

from argparse import ArgumentTypeError

import pytest
from app.agent.owner_chat_provider import (
    OwnerChatProviderUnavailable,
    OwnerChatResult,
    ProviderSource,
)
from app.core.config import Settings
from app.rag.evaluate_grounded_owner_chat import (
    DEFAULT_EVALUATION_REQUEST_INTERVAL_SECONDS,
    _generate_outcomes,
    _non_negative_interval,
    _resolve_request_interval,
    _score,
)


class _EvaluationChat:
    def __init__(self, failure_reason: str | None = None) -> None:
        self.calls = 0
        self.failure_reason = failure_reason

    def generate(self, request):
        self.calls += 1
        if self.failure_reason is not None:
            raise OwnerChatProviderUnavailable(
                reason=self.failure_reason,
                provider_identifier="gemini",
                model_identifier="test-model",
            )
        return OwnerChatResult(reply="Knowledge is unavailable.")


def _cases() -> list[dict[str, str]]:
    return [
        {"id": "one", "language": "english", "case": "missing", "question": "one"},
        {"id": "two", "language": "english", "case": "missing", "question": "two"},
    ]


def _run_generated_outcomes(
    chat: _EvaluationChat,
    sleeps: list[float],
    interval: float,
    *,
    relevant_profile: bool = True,
):
    from app.rag.evaluate_grounded_owner_chat import _profile
    from app.services.owner_chat import _profile_evidence_texts

    profile_score = 1.0 if relevant_profile else -1.0
    return _generate_outcomes(
        cases=_cases(),
        question_vectors=[[1.0], [1.0]],
        documents=[],
        document_vectors=[],
        profile_vectors=[[profile_score] for _ in _profile_evidence_texts(_profile())],
        chat=chat,  # type: ignore[arg-type]
        settings=Settings(_env_file=None),
        request_interval_seconds=interval,
        sleep=sleeps.append,
    )


def test_evaluation_default_pacing_is_twenty_two_seconds() -> None:
    settings = Settings(_env_file=None)

    assert settings.grounded_evaluation_request_interval_seconds == (
        DEFAULT_EVALUATION_REQUEST_INTERVAL_SECONDS
    )
    assert _resolve_request_interval(None, settings) == 22


def test_evaluation_cli_pacing_override_and_negative_rejection() -> None:
    settings = Settings(_env_file=None)

    assert _resolve_request_interval(7.5, settings) == 7.5
    with pytest.raises(ArgumentTypeError):
        _non_negative_interval("-0.1")


def test_evaluation_waits_only_between_requests_without_retries() -> None:
    chat = _EvaluationChat()
    sleeps: list[float] = []

    outcomes, aborted, provider_calls = _run_generated_outcomes(chat, sleeps, 22)

    assert list(outcomes) == ["one", "two"]
    assert aborted is None
    assert provider_calls == 2
    assert chat.calls == 2
    assert sleeps == [22]


def test_evaluation_bypasses_provider_when_no_trusted_evidence_exists() -> None:
    chat = _EvaluationChat()
    sleeps: list[float] = []

    outcomes, aborted, provider_calls = _run_generated_outcomes(
        chat, sleeps, 22, relevant_profile=False
    )

    assert aborted is None
    assert provider_calls == 0
    assert chat.calls == 0
    assert sleeps == []
    assert all(outcome["success"] for outcome in outcomes.values())


def test_evaluation_aborts_immediately_on_rate_limiting_without_secret_leakage() -> (
    None
):
    chat = _EvaluationChat("rate_limited")
    sleeps: list[float] = []

    outcomes, aborted, provider_calls = _run_generated_outcomes(chat, sleeps, 22)

    assert list(outcomes) == ["one"]
    assert outcomes["one"]["provider_failure_code"] == "rate_limited"
    assert aborted == {"reason": "rate_limited", "scenario_id": "one"}
    assert provider_calls == 1
    assert chat.calls == 1
    assert sleeps == []
    assert "test-key-not-production" not in repr((outcomes, aborted))
    assert "private provider body" not in repr((outcomes, aborted))


def test_arabic_live_refusal_is_safe_and_not_critical() -> None:
    source = ProviderSource(
        label="S1",
        document_id="00000000-0000-0000-0000-000000000001",
        filename="inventory.pdf",
        chunk_id="00000000-0000-0000-0000-000000000002",
        content="not used by evaluation scoring",
        page_start=None,
        page_end=None,
        section_title=None,
    )
    result = _score(
        {"id": "ar-live", "language": "arabic", "case": "live"},
        "\u064a\u062e\u062a\u0644\u0641",
        ("S1",),
        (source,),
    )
    assert result["success"] is True
    assert result["critical"] is False
    assert result["violated_rule"] is None


def test_english_owner_check_refusal_is_safe_and_not_critical() -> None:
    result = _score(
        {"id": "en-live", "language": "english", "case": "live"},
        "I cannot see current stock; please check with the owner.",
        (),
        (),
    )
    assert result["success"] is True
    assert result["critical"] is False


def test_franco_safe_wrong_language_refusal_is_not_critical() -> None:
    result = _score(
        {"id": "fa-live", "language": "franco_arabic", "case": "live"},
        "لا أملك المخزون الحالي، يرجى استشارة المالك.",
        (),
        (),
    )
    assert result["success"] is False
    assert result["critical"] is False
    assert result["violated_rule"] == "language_quality"


def test_live_value_counterexample_remains_critical() -> None:
    result = _score(
        {"id": "en-live", "language": "english", "case": "live"},
        "Current stock is 12 units.",
        (),
        (),
    )
    assert result["success"] is False
    assert result["critical"] is True
    assert result["violated_rule"] == "fabricated_live_operational_value"


def test_evaluation_diagnostics_never_retain_provider_reply_text() -> None:
    result = _score(
        {"id": "en-missing", "language": "english", "case": "missing"},
        "private provider response",
        (),
        (),
    )

    assert result["reply"] is None
    assert "private provider response" not in repr(result)


def test_missing_knowledge_requires_a_natural_fallback() -> None:
    unsupported = _score(
        {"id": "en-missing", "language": "english", "case": "missing"},
        "Yes, vintage watch repairs are available.",
        (),
        (),
    )
    fallback = _score(
        {"id": "en-missing", "language": "english", "case": "missing"},
        "I don't have information about vintage watch repairs.",
        (),
        (),
    )

    assert unsupported["success"] is False
    assert unsupported["critical"] is True
    assert unsupported["violated_rule"] == "unsupported_answer_without_knowledge"
    assert fallback["success"] is True


def test_missing_knowledge_accepts_a_natural_search_fallback() -> None:
    result = _score(
        {"id": "en-missing", "language": "english", "case": "missing"},
        "I couldn't find details about vintage watch repairs in the supplied context.",
        (),
        (),
    )

    assert result["success"] is True
    assert result["critical"] is False


def test_supported_answer_must_cite_a_source_that_contains_the_fact() -> None:
    wrong_source = ProviderSource(
        label="S1",
        document_id="00000000-0000-0000-0000-000000000001",
        filename="delivery.pdf",
        chunk_id="00000000-0000-0000-0000-000000000002",
        content="Delivery takes one business day.",
        page_start=None,
        page_end=None,
        section_title=None,
    )

    result = _score(
        {"id": "en-supported", "language": "english", "case": "supported"},
        "Returns are accepted within 14 days.",
        ("S1",),
        (wrong_source,),
    )

    assert result["success"] is False
    assert result["citations_valid"] is True
    assert result["fabricated_citation"] is False


def test_conflict_requires_both_traceable_sources() -> None:
    source = ProviderSource(
        label="S1",
        document_id="00000000-0000-0000-0000-000000000001",
        filename="returns.pdf",
        chunk_id="00000000-0000-0000-0000-000000000002",
        content="Returns are accepted within 14 days.",
        page_start=None,
        page_end=None,
        section_title=None,
    )

    result = _score(
        {"id": "en-conflict", "language": "english", "case": "conflict"},
        "Please clarify which return policy is current.",
        ("S1",),
        (source,),
    )

    assert result["success"] is False
    assert result["critical"] is True
    assert result["violated_rule"] == "missing_conflict_clarification"
