"""Recorded browser defects, exercised without a database or provider call."""

import socket
import uuid
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from app.agent.owner_chat_provider import (
    OwnerChatProviderInvalidResponse,
    OwnerChatRequest,
    OwnerChatResult,
    ProviderMessage,
    ProviderSource,
    TokenUsage,
)
from app.database.models import OperationalDataSourceStatus
from app.integrations.discovery import SourceMappingError
from app.services import (
    capabilities,
    catalogue_quality,
    data_sources,
    owner_chat,
    source_mapping,
)
from sqlalchemy import Engine
from test_catalogue_chat import NOW, PROFILE


@pytest.fixture(autouse=True)
def no_live_connections(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Browser regression tests cannot contact live services")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(Engine, "connect", forbidden)


def source(label="S1", content="Friday 08:15 - 12:45; 15:00 - 19:30"):
    return ProviderSource(
        label=label,
        document_id=str(uuid.uuid4()),
        chunk_id=str(uuid.uuid4()),
        filename="schedule.pdf",
        content=content,
        page_start=None,
        page_end=None,
        section_title=None,
    )


def request(
    question="What are Friday opening hours?", sources=None, language="english"
):
    return OwnerChatRequest(
        profile=PROFILE,
        knowledge=(),
        messages=(ProviderMessage(role="owner", content=question),),
        sources=sources or (source(),),
        requested_at=NOW,
        reply_language=language,
    )


@pytest.mark.parametrize(
    ("question", "language", "wrong_reply"),
    [
        (
            "What are Friday opening hours?",
            "english",
            "Friday 08:15 - 12:45 and 15:30 - 19:30 [S1]",
        ),
        (
            "شو أوقات الدوام نهار الجمعة؟",
            "lebanese_arabic",
            "من ٠٨:١٥ إلى ١٢:٤٥ ومن ١٥:٣٠ إلى ١٩:٣٠ [S1]",
        ),
        (
            "Sho aw2at el dawam yom el jem3a?",
            "franco_arabic",
            "men 08:15 la 12:45 w 15:30 la 19:30 [S1]",
        ),
    ],
)
def test_translated_hours_retain_the_source_values(question, language, wrong_reply):
    usage = TokenUsage(
        input_tokens=100, output_tokens=50, total_tokens=150, authoritative=True
    )
    result = owner_chat._enforce_grounded_clock_values(
        OwnerChatResult(reply=wrong_reply, cited_source_ids=("S1",), usage=usage),
        request(question, language=language),
    )
    assert "08:15 - 12:45; 15:00 - 19:30" in result.reply
    assert "15:30" not in result.reply
    assert result.usage is usage
    assert result.cited_source_ids == ("S1",)


def test_correct_unicode_clock_values_are_not_changed():
    result = OwnerChatResult(
        reply="٠٨:١٥ - ١٢:٤٥، ١٥:٠٠ - ١٩:٣٠ [S1]", cited_source_ids=("S1",)
    )
    assert owner_chat._enforce_grounded_clock_values(result, request()) is result


def test_clock_endpoints_cannot_be_reordered_even_when_all_values_are_supplied():
    result = owner_chat._enforce_grounded_clock_values(
        OwnerChatResult(reply="Friday 08:15 - 15:00; 12:45 - 19:30 [S1]"), request()
    )
    assert "08:15 - 12:45; 15:00 - 19:30" in result.reply


def test_conflicting_timetables_require_clarification_instead_of_choosing_one():
    result = owner_chat._enforce_grounded_clock_values(
        OwnerChatResult(reply="Friday 08:15 - 19:30 [S1]"),
        request(sources=(source(), source("S2", "Friday 10:00 - 18:00"))),
    )
    assert "conflicting" in result.reply
    assert result.cited_source_ids == ("S1", "S2")


def test_hours_guard_never_uses_previous_answers_as_authority():
    value = request()
    value = replace(
        value,
        messages=(
            ProviderMessage(
                role="assistant", content="Friday 08:15 - 12:45; 15:30 - 19:30"
            ),
            *value.messages,
        ),
    )
    result = owner_chat._enforce_grounded_clock_values(
        OwnerChatResult(reply="Friday 08:15 - 12:45; 15:30 - 19:30 [S1]"), value
    )
    assert "15:00" in result.reply and "15:30" not in result.reply


def test_hours_correction_keeps_usage_for_the_original_provider_output():
    response = OwnerChatResult(reply="Friday 08:15 - 12:45; 15:30 - 19:30 [S1]")
    provider = SimpleNamespace(
        generate=lambda _: response, estimate_input_tokens=lambda _: 101
    )
    original_usage = owner_chat._usage_for_result(provider, request(), response)
    result = owner_chat._validated_generation(provider, request())
    assert "15:00" in result.reply
    assert result.usage == original_usage


def test_every_inline_citation_is_persisted_under_its_original_source_label():
    sources = (source(), source("S5", "This is a synthetic demonstration."))
    result = owner_chat._validate_result(
        OwnerChatResult(
            reply="Published hours [S1]. Demo only [S5].", cited_source_ids=("S1",)
        ),
        request(sources=sources),
    )
    assert result.cited_source_ids == ("S1", "S5")


def test_unknown_inline_labels_fail_closed_instead_of_attaching_another_source():
    with pytest.raises(OwnerChatProviderInvalidResponse):
        owner_chat._validate_result(
            OwnerChatResult(reply="Unsupported [S5].", cited_source_ids=("S1",)),
            request(),
        )


def test_casual_conversation_never_gains_document_citations():
    value = replace(request(), mode="conversation", sources=())
    result = OwnerChatResult(reply="Hello")
    assert owner_chat._validate_result(result, value).cited_source_ids == ()


def test_expired_catalogue_approval_does_not_advertise_products_or_change_connectivity(
    monkeypatch,
):
    now = owner_chat.utc_now()
    value = SimpleNamespace(
        id=uuid.uuid4(),
        business_id=uuid.uuid4(),
        mapping_profile_key="discovered_products",
        mapping_version=5,
        status=OperationalDataSourceStatus.ACTIVE,
        adapter_type="sqlserver_readonly",
        connection_profile_key="reader",
        display_name="Offline source",
        last_validated_at=now,
        last_successful_health_check_at=now,
        failure_code=None,
        created_at=now,
        updated_at=now,
    )
    revision = SimpleNamespace(id=uuid.uuid4(), version=5)
    event = SimpleNamespace(
        report_id=uuid.uuid4(),
        approval_type="partial",
        expires_at=now - timedelta(seconds=1),
    )
    session = Mock()
    session.get.return_value = SimpleNamespace(revision_id=revision.id)
    monkeypatch.setattr(source_mapping, "latest_approved", lambda *_: revision)
    monkeypatch.setattr(capabilities, "active_capability_tools", lambda *_: ())
    monkeypatch.setattr(catalogue_quality, "latest_event", lambda *_: event)

    def expired(*_):
        raise SourceMappingError("catalogue_partial_approval_stale")

    monkeypatch.setattr(catalogue_quality, "approved_coverage", expired)
    result = data_sources._response(value, None, session)
    assert result.status == OperationalDataSourceStatus.ACTIVE
    assert result.quality_approval_status == "catalogue_partial_approval_expired"
    assert result.catalogue_approval_expires_at == event.expires_at
    assert "products" not in result.capabilities
    session.commit.assert_not_called()
