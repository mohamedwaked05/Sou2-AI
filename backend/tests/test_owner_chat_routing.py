"""Offline coverage for owner-chat intent routing and safe failures."""

import uuid

import pytest
from app.agent.owner_chat_provider import (
    OwnerChatProviderInvalidResponse,
    OwnerChatProviderTimeout,
    OwnerChatProviderUnavailable,
    OwnerChatRequest,
    OwnerChatResult,
)
from app.database.models import BusinessKnowledge, OwnerChatMessage
from app.rag.embeddings import EmbeddingResult
from app.services import owner_chat
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from tests.test_owner_chat import CapturingProvider, active_business, submit
from tests.test_rag_lifecycle import Provider, ready_document, vector


class _NoEvidenceProvider:
    model = "bge-m3"

    def embed(self, texts: list[str]) -> EmbeddingResult:
        question = tuple(vector(1))
        unrelated = (0.0, 1.0, *([0.0] * 1022))
        return EmbeddingResult(
            vectors=(question, *(unrelated for _ in texts[1:])),
            model=self.model,
        )


class _FailingChatProvider:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure
        self.calls = 0

    def estimate_input_tokens(self, request: OwnerChatRequest) -> int:
        return 20

    def generate(self, request: OwnerChatRequest) -> OwnerChatResult:
        self.calls += 1
        raise self.failure


@pytest.mark.parametrize(
    ("_language", "_intent", "message"),
    [
        ("english", "greeting", "Hello!"),
        ("english", "thanks", "Thank you"),
        ("english", "acknowledgement", "Got it"),
        ("english", "wellbeing", "How are you?"),
        ("english", "goodbye", "See you"),
        ("arabic", "greeting", "مرحبا"),
        ("arabic", "thanks", "شكراً"),
        ("arabic", "acknowledgement", "حسناً"),
        ("arabic", "wellbeing", "كيف حالك؟"),
        ("arabic", "goodbye", "مع السلامة"),
        ("lebanese_arabic", "greeting", "أهلين"),
        ("lebanese_arabic", "thanks", "مرسي"),
        ("lebanese_arabic", "acknowledgement", "تمام"),
        ("lebanese_arabic", "wellbeing", "كيفك؟"),
        ("lebanese_arabic", "goodbye", "يلا باي"),
        ("franco_arabic", "greeting", "marhaba"),
        ("franco_arabic", "thanks", "merci"),
        ("franco_arabic", "acknowledgement", "tamam"),
        ("franco_arabic", "wellbeing", "kifak?"),
        ("franco_arabic", "goodbye", "yalla bye"),
        ("mixed", "greeting", "Hi مرحبا"),
        ("mixed", "thanks", "Thanks شكراً"),
        ("mixed", "acknowledgement", "okay تمام"),
        ("mixed", "wellbeing", "Hi كيفك؟"),
        ("mixed", "goodbye", "bye باي"),
    ],
)
def test_ordinary_casual_phrases_do_not_require_business_evidence(
    _language: str, _intent: str, message: str
) -> None:
    assert owner_chat._requires_business_evidence(message) is False


@pytest.mark.parametrize(
    "message",
    [
        "How many Pepsi do we have left?",
        "Do we have iPad Pro available?",
        "What is the quantity of P1001?",
        "How much WATER-1500 remains?",
        "قديش عنا بيبسي",
        "كم آيباد باقي",
        "هل المنتج P1001 متوفر",
        "قديش باقي من هيدا المنتج",
        "adde 3anna Pepsi?",
        "kam iPad ba2e?",
        "fi P1001 available?",
        "adde ba2e men WATER-1500?",
    ],
)
def test_multilingual_product_quantity_phrasing_routes_only_to_inventory(
    message: str,
) -> None:
    assert owner_chat._is_live_operational_request(message) is True
    assert owner_chat._matching_operational_tools(message) == frozenset(
        {"current_inventory"}
    )


@pytest.mark.parametrize(
    "message",
    [
        "How many days do we have left?",
        "How many employees do we have?",
        "kam meeting ba2e?",
        "كم مواعيد باقي",
    ],
)
def test_unrelated_how_many_phrasing_is_not_a_product_inventory_query(
    message: str,
) -> None:
    assert "inventory" not in owner_chat._query_concepts(message)


def test_casual_turn_uses_provider_once_and_replays_without_duplicate_usage(
    api_client,
    db_session: Session,
    migration_engine: Engine,
    monkeypatch,
) -> None:
    user, business = active_business(
        api_client, db_session, email="casual-routing@example.com"
    )
    provider = CapturingProvider(OwnerChatResult(reply="Hello! How are things?"))
    monkeypatch.setattr(
        owner_chat,
        "create_embedding_provider",
        lambda _settings: (_ for _ in ()).throw(
            AssertionError("casual routing must bypass embeddings and retrieval")
        ),
    )
    from app.agent.owner_chat_provider import get_owner_chat_provider
    from app.main import app

    app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    first = submit(api_client, user, business["id"], "Hello!", key="casual")
    replay = submit(api_client, user, business["id"], "Hello!", key="casual")

    assert first.status_code == replay.status_code == 200
    payload = first.json()
    assert payload["assistant_message"]["content"] == "Hello! How are things?"
    assert payload["assistant_message"]["sources"] == []
    assert (
        replay.json()["assistant_message"]["id"] == payload["assistant_message"]["id"]
    )
    assert replay.json()["replayed"] is True
    assert len(provider.requests) == 1
    assert provider.requests[0].mode == "conversation"
    assert provider.requests[0].knowledge == provider.requests[0].sources == ()
    owner = db_session.get(OwnerChatMessage, uuid.UUID(payload["owner_message"]["id"]))
    assert owner is not None
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(OwnerChatMessage)
            .where(OwnerChatMessage.conversation_id == owner.conversation_id)
        )
        == 2
    )
    business_id = uuid.UUID(str(business["id"]))
    with migration_engine.connect() as connection:
        reservations = connection.scalar(
            text(
                "SELECT count(*) FROM ai_usage_reservations "
                "WHERE business_id = :business_id"
            ),
            {"business_id": business_id},
        )
        usage = connection.scalar(
            text(
                "SELECT count(*) FROM business_ai_usage_daily "
                "WHERE business_id = :business_id"
            ),
            {"business_id": business_id},
        )
        rate_events = connection.scalar(
            text(
                "SELECT count(*) FROM owner_chat_rate_limit_events "
                "WHERE business_id = :business_id"
            ),
            {"business_id": business_id},
        )
    assert reservations == usage == rate_events == 1


def test_live_request_has_priority_over_greeting(
    api_client, db_session: Session, monkeypatch
) -> None:
    user, business = active_business(
        api_client, db_session, email="casual-live-priority@example.com"
    )
    provider = CapturingProvider(OwnerChatResult(reply="must not run"))
    monkeypatch.setattr(
        owner_chat,
        "create_embedding_provider",
        lambda _settings: (_ for _ in ()).throw(
            AssertionError("live routing must bypass embeddings")
        ),
    )
    from app.agent.owner_chat_provider import get_owner_chat_provider
    from app.main import app

    app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    response = submit(
        api_client,
        user,
        business["id"],
        "Hi, what is my current stock?",
        key="live-priority",
    )

    assert response.status_code == 200
    assert (
        "can't access live operational data"
        in response.json()["assistant_message"]["content"]
    )
    assert response.json()["assistant_message"]["sources"] == []
    assert provider.requests == []


def test_business_question_has_priority_over_thanks_and_uses_rag(
    api_client, db_session: Session, monkeypatch
) -> None:
    user, business = active_business(
        api_client, db_session, email="casual-rag-priority@example.com"
    )
    business_id = uuid.UUID(str(business["id"]))
    ready_document(
        db_session,
        business_id,
        digest="5" * 64,
        chunks=[("Returns are accepted within 14 days.", vector(1), "bge-m3")],
    )
    provider = CapturingProvider(
        OwnerChatResult(reply="Returns take 14 days.", cited_source_ids=("S1",))
    )
    monkeypatch.setattr(owner_chat, "create_embedding_provider", lambda _: Provider())
    from app.agent.owner_chat_provider import get_owner_chat_provider
    from app.main import app

    app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    response = submit(
        api_client,
        user,
        business_id,
        "Thanks, what is our return policy?",
        key="rag-priority",
    )

    assert response.status_code == 200, response.text
    assert response.json()["assistant_message"]["content"] == "Returns take 14 days."
    assert response.json()["assistant_message"]["sources"][0]["label"] == "S1"
    assert len(provider.requests) == 1
    assert provider.requests[0].mode == "grounded"


def test_greeting_prefix_does_not_hide_missing_business_knowledge(
    api_client, db_session: Session, monkeypatch
) -> None:
    user, business = active_business(
        api_client, db_session, email="casual-missing-priority@example.com"
    )
    provider = CapturingProvider(OwnerChatResult(reply="must not run"))
    monkeypatch.setattr(
        owner_chat, "create_embedding_provider", lambda _: _NoEvidenceProvider()
    )
    from app.agent.owner_chat_provider import get_owner_chat_provider
    from app.main import app

    app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    response = submit(
        api_client,
        user,
        business["id"],
        "Hello, do you repair antique clocks?",
        key="missing-priority",
    )

    assert response.status_code == 200
    assert "don't have information" in response.json()["assistant_message"]["content"]
    assert response.json()["assistant_message"]["sources"] == []
    assert provider.requests == []


def test_general_advice_uses_conversation_mode_without_rag_or_citations(
    api_client, db_session: Session, monkeypatch
) -> None:
    user, business = active_business(
        api_client, db_session, email="general-advice@example.com"
    )
    provider = CapturingProvider(
        OwnerChatResult(reply="Break the goal into small, measurable steps.")
    )
    monkeypatch.setattr(
        owner_chat,
        "create_embedding_provider",
        lambda _settings: (_ for _ in ()).throw(
            AssertionError("conversation mode must bypass RAG")
        ),
    )
    from app.agent.owner_chat_provider import get_owner_chat_provider
    from app.main import app

    app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    response = submit(
        api_client,
        user,
        business["id"],
        "How can I stay motivated while working on a long project?",
        key="general-advice",
    )

    assert response.status_code == 200, response.text
    assert response.json()["assistant_message"]["content"] == (
        "Break the goal into small, measurable steps."
    )
    assert response.json()["assistant_message"]["sources"] == []
    assert len(provider.requests) == 1
    assert provider.requests[0].mode == "conversation"
    assert provider.requests[0].sources == provider.requests[0].knowledge == ()
    assert db_session.scalar(select(func.count()).select_from(BusinessKnowledge)) == 0


@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_message"),
    [
        (
            OwnerChatProviderUnavailable(reason="rate_limited", usage_uncertain=False),
            "assistant_rate_limited",
            "The assistant is handling too many requests right now. "
            "Please try again later.",
        ),
        (
            OwnerChatProviderTimeout(reason="timeout", usage_uncertain=False),
            "assistant_timeout",
            "The assistant took too long to respond. Please try again.",
        ),
        (
            OwnerChatProviderUnavailable(
                reason="transport_failure", usage_uncertain=False
            ),
            "assistant_transport_failure",
            "The assistant cannot be reached right now. Please try again.",
        ),
        (
            OwnerChatProviderInvalidResponse(
                reason="private provider body", usage_uncertain=False
            ),
            "assistant_invalid_response",
            "The assistant returned an unusable response. Please try again.",
        ),
    ],
)
def test_provider_failures_have_safe_accurate_api_errors(
    api_client,
    db_session: Session,
    monkeypatch,
    failure: Exception,
    expected_code: str,
    expected_message: str,
) -> None:
    user, business = active_business(
        api_client,
        db_session,
        email=f"provider-{expected_code}@example.com",
    )
    provider = _FailingChatProvider(failure)
    monkeypatch.setattr(
        owner_chat,
        "create_embedding_provider",
        lambda _settings: (_ for _ in ()).throw(
            AssertionError("general conversation must bypass RAG")
        ),
    )
    from app.agent.owner_chat_provider import get_owner_chat_provider
    from app.main import app

    app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    response = submit(
        api_client,
        user,
        business["id"],
        "How can I stay focused on a difficult project?",
        key=expected_code,
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == expected_code
    assert response.json()["error"]["message"] == expected_message
    assert "private provider body" not in response.text
    assert "gemini" not in response.text.casefold()
    assert provider.calls == 1
