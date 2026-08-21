"""Regression coverage for safe grounded-evaluation diagnostics."""

from app.agent.owner_chat_provider import ProviderSource
from app.rag.evaluate_grounded_owner_chat import _score


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
