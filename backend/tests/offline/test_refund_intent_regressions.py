"""Refund totals must not fall back to policy knowledge; no live I/O."""

import pytest
from app.services.owner_chat import (
    _capability_access_reply,
    _is_capability_access_request,
    _is_catalogue_lookup_request,
    _is_live_operational_request,
)


@pytest.mark.parametrize(
    "question",
    [
        "How much was refunded?",
        "What were our total refunds last month?",
        "قديش رجعنا مصاري مبارح؟",
        "كم المبلغ المسترد الشهر الماضي؟",
        "adde raja3na masare mbere7?",
        "2adde refunds last month?",
    ],
)
def test_refund_aggregates_require_live_operational_data(question):
    assert _is_live_operational_request(question)


@pytest.mark.parametrize(
    "question",
    [
        "What is our refund policy?",
        "Explain how refunds work.",
        "شو سياسة الترجيع؟",
        "shu siyaset el tarji3?",
    ],
)
def test_refund_policy_remains_a_knowledge_question(question):
    assert not _is_live_operational_request(question)


@pytest.mark.parametrize(
    "question",
    [
        "What is the recorded Selling1 price of Pepsi Diet Mini?",
        "How many Paid SI documents were recorded "
        "from February 1 through February 16, 2023?",
        "Count Paid SI headers for January 2023",
        "adde se3er Pepsi Diet Mini?",
    ],
)
def test_reviewed_source_operations_cannot_fall_back_to_knowledge(question):
    assert _is_live_operational_request(question)


def test_capability_access_uses_available_operations_without_inventing_approval():
    question = "What information can you access for Hilaleyih?"
    assert _is_capability_access_request(question)
    assert not _is_capability_access_request("What is our refund policy?")
    unavailable = _capability_access_reply(set(), question, "en")
    assert "No approved operational capability" in unavailable
    assert "Data Sources" in unavailable
    catalogue_only = _capability_access_reply({"products"}, question, "en")
    assert "Currently available operational capabilities: catalogue." in catalogue_only
    assert "balances" not in catalogue_only


@pytest.mark.parametrize(
    "question",
    [
        "شو عنا منتجات بيبسي؟",
        "ما عندكم منتجات؟",
        "shu 3anna Pepsi products?",
        "shu 3andkon Pepsi?",
        "What about the other Pepsi variants?",
    ],
)
def test_multilingual_catalogue_discovery_requires_approved_source(question):
    assert _is_catalogue_lookup_request(question)
