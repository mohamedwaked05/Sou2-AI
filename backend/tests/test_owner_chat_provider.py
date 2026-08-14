"""Provider selection and local Ollama contract tests without network access."""

import json
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

import httpx
import pytest
from app.agent import owner_chat_provider
from app.agent.owner_chat_provider import (
    DeterministicMockOwnerChatProvider,
    OllamaOwnerChatProvider,
    OwnerChatProvider,
    OwnerChatProviderError,
    OwnerChatProviderInvalidResponse,
    OwnerChatProviderTimeout,
    OwnerChatProviderUnavailable,
    OwnerChatRequest,
    OwnerChatResult,
    ProviderBusinessProfile,
    ProviderKnowledge,
    ProviderMessage,
    ProviderWorkingDay,
    ProviderWorkingShift,
    create_owner_chat_provider,
    estimate_utf8_tokens,
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


def maximal_provider_request() -> OwnerChatRequest:
    original = provider_request()
    multilingual = 'English "quote" \\ control\n العربية Franco 3arabi'
    profile = replace(
        original.profile,
        description=multilingual * 20,
        working_hours=tuple(
            ProviderWorkingDay(
                weekday=weekday,
                is_open=True,
                shifts=(
                    ProviderWorkingShift(time(8), time(12)),
                    ProviderWorkingShift(time(13), time(18)),
                ),
            )
            for weekday in (
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
            )
        ),
    )
    expiry = original.requested_at + timedelta(days=1)
    knowledge = tuple(
        ProviderKnowledge(
            subject_key=f"policy_{index}",
            content=f"{multilingual} {index}",
            category="promotion" if index % 2 else "policy",
            expires_at=expiry if index % 2 else None,
        )
        for index in range(100)
    )
    messages = tuple(
        ProviderMessage(
            role="owner" if index % 2 == 0 else "assistant",
            content=f"{multilingual} message {index}",
        )
        for index in range(12)
    )
    return replace(
        original,
        profile=profile,
        knowledge=knowledge,
        messages=messages,
    )


def test_mock_provider_selection_is_explicit_and_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OWNER_CHAT_PROVIDER", raising=False)

    provider = create_owner_chat_provider(
        Settings(_env_file=None, owner_chat_provider="mock")
    )

    assert isinstance(provider, DeterministicMockOwnerChatProvider)


def test_ollama_provider_selection_does_not_contact_service() -> None:
    transport = httpx.MockTransport(
        lambda request: pytest.fail("Provider selection must not perform HTTP I/O")
    )
    settings = Settings(owner_chat_provider="ollama")

    provider = create_owner_chat_provider(settings, transport=transport)

    assert isinstance(provider, OllamaOwnerChatProvider)
    assert provider.model == "qwen2.5:7b"


def test_default_provider_selection_is_ollama_without_startup_io() -> None:
    transport = httpx.MockTransport(
        lambda request: pytest.fail("Provider selection must not perform HTTP I/O")
    )

    provider = create_owner_chat_provider(Settings(_env_file=None), transport=transport)

    assert isinstance(provider, OllamaOwnerChatProvider)


@pytest.fixture(params=["mock", "ollama"])
def contract_provider(request: pytest.FixtureRequest) -> OwnerChatProvider:
    if request.param == "mock":
        return DeterministicMockOwnerChatProvider()
    return ollama_provider(
        successful_transport({"reply": "Safe visible reply.", "proposed_knowledge": []})
    )


def test_shared_provider_contract(
    contract_provider: OwnerChatProvider,
) -> None:
    request = maximal_provider_request()
    original_request = request

    assert isinstance(contract_provider, OwnerChatProvider)
    assert contract_provider.estimate_input_tokens(request) > 0
    assert contract_provider.estimate_input_tokens(request) == (
        contract_provider.estimate_input_tokens(request)
    )

    result = contract_provider.generate(request)

    assert isinstance(result, OwnerChatResult)
    assert result.reply.strip()
    assert result.proposed_knowledge == ()
    assert isinstance(result.provider_identifier, str) and result.provider_identifier
    assert isinstance(result.model_identifier, str) and result.model_identifier
    assert result.usage is not None
    assert result.usage.input_tokens >= 0
    assert result.usage.output_tokens >= 0
    assert result.usage.total_tokens == (
        result.usage.input_tokens + result.usage.output_tokens
    )
    assert result.usage.output_tokens <= request.max_output_tokens
    assert request == original_request


def test_mock_provider_is_network_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        owner_chat_provider.httpx,
        "Client",
        lambda *args, **kwargs: pytest.fail(
            "Mock provider must not create an HTTP client"
        ),
    )

    result = DeterministicMockOwnerChatProvider().generate(provider_request())

    assert result.provider_identifier == "mock"


def test_mock_provider_honors_small_output_limit() -> None:
    request = replace(provider_request(), max_output_tokens=1)

    result = DeterministicMockOwnerChatProvider().generate(request)

    assert result.usage is not None
    assert result.usage.output_tokens <= request.max_output_tokens


@pytest.mark.parametrize(
    ("behavior", "error_type"),
    [
        ("timeout", OwnerChatProviderTimeout),
        ("unavailable", OwnerChatProviderUnavailable),
        ("invalid", OwnerChatProviderInvalidResponse),
    ],
)
def test_mock_failures_keep_safe_accounting_identifiers(
    behavior: str,
    error_type: type[OwnerChatProviderError],
) -> None:
    provider = DeterministicMockOwnerChatProvider(behavior=behavior)  # type: ignore[arg-type]

    with pytest.raises(error_type) as raised:
        provider.generate(provider_request())

    error = raised.value
    assert error.provider_identifier == "mock"
    assert error.model_identifier == "deterministic"
    assert error.usage_uncertain is True


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


def test_provider_estimate_and_fallback_share_complete_canonical_input() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {"reply": "Complete response.", "proposed_knowledge": []}
                    ),
                }
            },
        )

    request = maximal_provider_request()
    provider = ollama_provider(httpx.MockTransport(handler))
    estimate = provider.estimate_input_tokens(request)
    result = provider.generate(request)
    canonical_payload = json.dumps(
        captured["payload"],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert result.usage is not None
    assert result.usage.authoritative is False
    assert result.usage.input_tokens == estimate
    assert estimate == estimate_utf8_tokens(canonical_payload)
    assert estimate > estimate_utf8_tokens(
        "".join(message.content for message in request.messages)
    )

    mock = DeterministicMockOwnerChatProvider()
    mock_result = mock.generate(request)
    assert mock_result.usage is not None
    assert mock_result.usage.input_tokens == mock.estimate_input_tokens(request)


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


@pytest.mark.parametrize(
    ("transport", "error_type", "usage_uncertain"),
    [
        (
            httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(
                    httpx.ConnectError("refused", request=request)
                )
            ),
            OwnerChatProviderUnavailable,
            False,
        ),
        (
            httpx.MockTransport(
                lambda request: httpx.Response(
                    404, json={"error": "model qwen2.5:7b not found"}
                )
            ),
            OwnerChatProviderUnavailable,
            False,
        ),
        (
            httpx.MockTransport(
                lambda request: httpx.Response(
                    500, json={"error": "model qwen2.5:7b not found"}
                )
            ),
            OwnerChatProviderUnavailable,
            True,
        ),
        (
            httpx.MockTransport(
                lambda request: httpx.Response(
                    503, json={"error": "model does not exist"}
                )
            ),
            OwnerChatProviderUnavailable,
            True,
        ),
        (
            httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(
                    httpx.ReadError("connection reset", request=request)
                )
            ),
            OwnerChatProviderUnavailable,
            True,
        ),
        (
            httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(
                    httpx.ReadTimeout("slow", request=request)
                )
            ),
            OwnerChatProviderTimeout,
            True,
        ),
    ],
)
def test_ollama_failure_accounting_classification(
    transport: httpx.MockTransport,
    error_type: type[OwnerChatProviderUnavailable],
    usage_uncertain: bool,
) -> None:
    with pytest.raises(error_type) as raised:
        ollama_provider(transport).generate(provider_request())
    assert raised.value.usage is None
    assert raised.value.usage_uncertain is usage_uncertain


def test_invalid_structured_response_preserves_authoritative_usage() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": "not-json"},
                "prompt_eval_count": 44,
                "eval_count": 9,
            },
        )
    )
    with pytest.raises(OwnerChatProviderInvalidResponse) as raised:
        ollama_provider(transport).generate(provider_request())
    assert raised.value.usage is not None
    assert raised.value.usage.total_tokens == 53
    assert raised.value.usage.authoritative is True


def test_http_failure_preserves_authoritative_usage() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            500,
            json={
                "error": "model qwen2.5:7b not found",
                "prompt_eval_count": 31,
                "eval_count": 7,
            },
        )
    )
    with pytest.raises(OwnerChatProviderUnavailable) as raised:
        ollama_provider(transport).generate(provider_request())
    assert raised.value.usage is not None
    assert raised.value.usage.total_tokens == 38
    assert raised.value.usage.authoritative is True
    assert raised.value.usage_uncertain is True


def test_invalid_response_without_usage_is_uncertain() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"not-json")
    )
    with pytest.raises(OwnerChatProviderInvalidResponse) as raised:
        ollama_provider(transport).generate(provider_request())
    assert raised.value.usage is None
    assert raised.value.usage_uncertain is True


@pytest.mark.parametrize(
    ("status_code", "expected_reason"),
    [(404, "model_missing"), (500, "http_error")],
)
def test_http_errors_log_only_safe_machine_reason(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected_reason: str,
) -> None:
    logged: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        owner_chat_provider.logger,
        "warning",
        lambda *arguments: logged.append(arguments),
    )
    private_error = "model qwen2.5:7b not found; private-provider-detail"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status_code, json={"error": private_error})
    )

    with pytest.raises(OwnerChatProviderUnavailable):
        ollama_provider(transport).generate(provider_request())

    assert logged == [("Owner chat provider failed: reason=%s", expected_reason)]
    assert private_error not in repr(logged)
