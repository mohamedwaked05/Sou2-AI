"""Provider selection and local Ollama contract tests without network access."""

import json
from datetime import UTC, datetime, time

import httpx
import pytest
from app.agent import owner_chat_provider
from app.agent.owner_chat_provider import (
    DeterministicMockOwnerChatProvider,
    OllamaOwnerChatProvider,
    OwnerChatProviderInvalidResponse,
    OwnerChatProviderTimeout,
    OwnerChatProviderUnavailable,
    OwnerChatRequest,
    ProviderBusinessProfile,
    ProviderKnowledge,
    ProviderMessage,
    ProviderWorkingDay,
    ProviderWorkingShift,
    create_owner_chat_provider,
)
from app.core.config import Settings
from pydantic import ValidationError


def provider_request() -> OwnerChatRequest:
    return OwnerChatRequest(
        profile=ProviderBusinessProfile(
            name="Tenant Market",
            description="A complete neighborhood grocery business profile.",
            category="GROCERY_SUPERMARKET",
            governorate="Beirut",
            district="Beirut",
            city="Beirut",
            address_line="Hamra Street, building 10",
            timezone="Asia/Beirut",
            working_hours=(
                ProviderWorkingDay(
                    weekday="monday",
                    is_open=True,
                    shifts=(ProviderWorkingShift(time(9), time(18)),),
                ),
                ProviderWorkingDay(
                    weekday="tuesday",
                    is_open=True,
                    shifts=(ProviderWorkingShift(time(9), time(18)),),
                ),
                ProviderWorkingDay(
                    weekday="wednesday",
                    is_open=True,
                    shifts=(ProviderWorkingShift(time(9), time(18)),),
                ),
                ProviderWorkingDay(
                    weekday="thursday",
                    is_open=True,
                    shifts=(ProviderWorkingShift(time(9), time(18)),),
                ),
                ProviderWorkingDay(
                    weekday="friday",
                    is_open=True,
                    shifts=(ProviderWorkingShift(time(9), time(18)),),
                ),
                ProviderWorkingDay(
                    weekday="saturday",
                    is_open=True,
                    shifts=(
                        ProviderWorkingShift(time(9), time(12)),
                        ProviderWorkingShift(time(13), time(14)),
                    ),
                ),
                ProviderWorkingDay(weekday="sunday", is_open=False),
            ),
        ),
        knowledge=(
            ProviderKnowledge(
                subject_key="return_policy",
                content="Returns are accepted within seven days.",
                category="returns",
                expires_at=None,
            ),
        ),
        messages=(
            ProviderMessage(role="owner", content="What is our return policy?"),
            ProviderMessage(
                role="assistant", content="Returns are accepted within seven days."
            ),
            ProviderMessage(role="owner", content="Please remind me again."),
        ),
        requested_at=datetime(2026, 8, 13, 10, tzinfo=UTC),
    )


def successful_transport(structured: dict[str, object]) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": json.dumps(structured)}},
        )
    )


def ollama_provider(transport: httpx.MockTransport) -> OllamaOwnerChatProvider:
    return OllamaOwnerChatProvider(
        base_url="http://ollama.invalid",
        model="qwen2.5:7b",
        timeout_seconds=120,
        transport=transport,
    )


@pytest.mark.parametrize("configured", [None, "mock"])
def test_mock_provider_selection_is_default_and_offline(
    configured: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OWNER_CHAT_PROVIDER", raising=False)
    values = {} if configured is None else {"owner_chat_provider": configured}

    provider = create_owner_chat_provider(Settings(_env_file=None, **values))

    assert isinstance(provider, DeterministicMockOwnerChatProvider)


def test_ollama_provider_selection_does_not_contact_service() -> None:
    transport = httpx.MockTransport(
        lambda request: pytest.fail("Provider selection must not perform HTTP I/O")
    )
    settings = Settings(owner_chat_provider="ollama")

    provider = create_owner_chat_provider(settings, transport=transport)

    assert isinstance(provider, OllamaOwnerChatProvider)
    assert provider.model == "qwen2.5:7b"


def test_unknown_provider_and_nonpositive_timeout_are_rejected() -> None:
    with pytest.raises(ValidationError, match="owner_chat_provider"):
        Settings(owner_chat_provider="unknown")
    with pytest.raises(ValidationError, match="ollama_request_timeout_seconds"):
        Settings(ollama_request_timeout_seconds=0)
    with pytest.raises(ValidationError, match="GENERATION_LEASE_SECONDS"):
        Settings(
            owner_chat_provider="ollama",
            owner_chat_generation_lease_seconds=120,
            ollama_request_timeout_seconds=120,
        )


def test_ollama_request_uses_configured_context_model_and_timeout() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        captured["timeout"] = request.extensions["timeout"]
        content = {
            "reply": "The return policy is seven days.",
            "proposed_knowledge": [],
        }
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": json.dumps(content)}},
        )

    provider = OllamaOwnerChatProvider(
        base_url="http://ollama.test:11434",
        model="configured-model:7b",
        timeout_seconds=37,
        transport=httpx.MockTransport(handler),
    )

    result = provider.generate(provider_request())

    assert result.reply == "The return policy is seven days."
    assert captured["url"] == "http://ollama.test:11434/api/chat"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "configured-model:7b"
    assert payload["stream"] is False
    assert payload["options"] == {"num_predict": 512}
    assert isinstance(payload["format"], dict)
    assert [message["role"] for message in payload["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert [message["content"] for message in payload["messages"][1:]] == [
        "What is our return policy?",
        "Returns are accepted within seven days.",
        "Please remind me again.",
    ]
    system = payload["messages"][0]["content"]
    assert "Tenant Market" in system
    assert "return_policy" in system
    assert "`reply` is the natural-language answer" in system
    assert "`category` is metadata" in system
    assert "complete working hours" in system
    assert "temporary_notice, promotion, delivery, or returns" in system
    assert "Never invent current stock, revenue, orders, sales" in system
    assert "Ask the owner for clarification" in system
    assert "business_id" not in system
    assert "Foreign Tenant" not in system
    context = json.loads(system.split("Trusted tenant context follows:\n", 1)[1])
    working_hours = context["business_profile"]["working_hours"]
    assert [day["weekday"] for day in working_hours] == [
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]
    assert working_hours[5] == {
        "weekday": "saturday",
        "is_open": True,
        "shifts": [
            {"start": "09:00", "end": "12:00"},
            {"start": "13:00", "end": "14:00"},
        ],
    }
    assert working_hours[6] == {
        "weekday": "sunday",
        "is_open": False,
        "shifts": [],
    }
    timeout = captured["timeout"]
    assert isinstance(timeout, dict)
    assert set(timeout.values()) == {37}


def test_ollama_authoritative_usage_is_provider_neutral() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {"reply": "Counted response.", "proposed_knowledge": []}
                    ),
                },
                "prompt_eval_count": 123,
                "eval_count": 17,
            },
        )
    )
    result = ollama_provider(transport).generate(provider_request())
    assert result.usage is not None
    assert result.usage.input_tokens == 123
    assert result.usage.output_tokens == 17
    assert result.usage.total_tokens == 140
    assert result.usage.authoritative is True
    assert result.provider_identifier == "ollama"
    assert result.model_identifier == "qwen2.5:7b"


@pytest.mark.parametrize(
    ("structured", "expected_count"),
    [
        ({"reply": "Hello owner.", "proposed_knowledge": []}, 0),
        (
            {
                "reply": "I recorded the delivery policy.",
                "proposed_knowledge": [
                    {
                        "subject_key": "delivery_charge",
                        "content": "Delivery costs 5 USD.",
                        "kind": "permanent",
                        "category": "delivery",
                        "expires_at": None,
                    }
                ],
            },
            1,
        ),
        (
            {
                "reply": "I recorded today's closing notice.",
                "proposed_knowledge": [
                    {
                        "subject_key": "closing_notice",
                        "content": "The business closes at 4 PM today.",
                        "kind": "temporary",
                        "category": "temporary_notice",
                        "expires_at": "2026-08-13T20:59:59+00:00",
                    }
                ],
            },
            1,
        ),
    ],
)
def test_valid_structured_responses_use_neutral_contract(
    structured: dict[str, object], expected_count: int
) -> None:
    transport = successful_transport(structured)
    provider = ollama_provider(transport)

    result = provider.generate(provider_request())

    assert result.reply == structured["reply"]
    assert len(result.proposed_knowledge) == expected_count
    if expected_count:
        source = structured["proposed_knowledge"][0]
        fact = result.proposed_knowledge[0]
        assert fact.subject_key == source["subject_key"]
        assert fact.kind == source["kind"]


def test_hidden_reasoning_fields_are_ignored() -> None:
    structured = {
        "reply": "Safe visible reply.",
        "proposed_knowledge": [],
    }
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": json.dumps(structured),
                    "thinking": "Hidden Ollama reasoning",
                }
            },
        )
    )

    result = ollama_provider(transport).generate(provider_request())

    assert result.reply == "Safe visible reply."
    assert "reasoning" not in result.reply
    assert result.proposed_knowledge == ()


@pytest.mark.parametrize(
    ("transport", "error_type"),
    [
        (
            httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(
                    httpx.ConnectError("refused", request=request)
                )
            ),
            OwnerChatProviderUnavailable,
        ),
        (
            httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(
                    httpx.ReadTimeout("slow", request=request)
                )
            ),
            OwnerChatProviderTimeout,
        ),
        (
            httpx.MockTransport(
                lambda request: httpx.Response(
                    404, json={"error": "model 'qwen2.5:7b' not found"}
                )
            ),
            OwnerChatProviderUnavailable,
        ),
        (
            httpx.MockTransport(
                lambda request: httpx.Response(500, text="internal failure")
            ),
            OwnerChatProviderUnavailable,
        ),
        (
            httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"not-json")
            ),
            OwnerChatProviderInvalidResponse,
        ),
        (
            httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "message": {
                            "role": "user",
                            "content": json.dumps(
                                {"reply": "Wrong role", "proposed_knowledge": []}
                            ),
                        }
                    },
                )
            ),
            OwnerChatProviderInvalidResponse,
        ),
        (
            successful_transport({"reply": "", "proposed_knowledge": []}),
            OwnerChatProviderInvalidResponse,
        ),
        (
            successful_transport(
                {"reply": "temporary_notice", "proposed_knowledge": []}
            ),
            OwnerChatProviderInvalidResponse,
        ),
        (
            successful_transport(
                {
                    "reply": "Visible reply",
                    "proposed_knowledge": [],
                    "reasoning": "unexpected field",
                }
            ),
            OwnerChatProviderInvalidResponse,
        ),
        (
            successful_transport({"proposed_knowledge": []}),
            OwnerChatProviderInvalidResponse,
        ),
        (
            successful_transport({"reply": "Missing required fact list"}),
            OwnerChatProviderInvalidResponse,
        ),
        (
            successful_transport(
                {
                    "reply": "Visible reply",
                    "proposed_knowledge": [
                        {
                            "subject_key": "notice",
                            "content": "Temporary notice",
                            "kind": "temporary",
                            "category": "temporary_notice",
                            "unexpected": "malformed",
                        }
                    ],
                }
            ),
            OwnerChatProviderInvalidResponse,
        ),
    ],
)
def test_ollama_failures_map_to_safe_provider_errors(
    transport: httpx.MockTransport, error_type: type[Exception]
) -> None:
    with pytest.raises(error_type):
        ollama_provider(transport).generate(provider_request())


def test_missing_model_logs_only_safe_machine_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logged: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        owner_chat_provider.logger,
        "warning",
        lambda *arguments: logged.append(arguments),
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            404, json={"error": "model qwen2.5:7b not found"}
        )
    )

    with pytest.raises(OwnerChatProviderUnavailable):
        ollama_provider(transport).generate(provider_request())

    assert logged == [("Owner chat provider failed: reason=%s", "model_missing")]
