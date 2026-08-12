"""Provider-neutral owner-chat generation contract and deterministic mock."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class OwnerChatProviderError(Exception):
    """Base class for safe provider failures."""


class OwnerChatProviderTimeout(OwnerChatProviderError):
    """The provider did not return within its configured deadline."""


class OwnerChatProviderUnavailable(OwnerChatProviderError):
    """The provider is temporarily unavailable."""


class OwnerChatProviderInvalidResponse(OwnerChatProviderError):
    """The provider returned an unusable response."""


@dataclass(frozen=True)
class ProviderBusinessProfile:
    name: str
    description: str
    category: str
    governorate: str
    district: str
    city: str
    address_line: str
    timezone: str


@dataclass(frozen=True)
class ProviderMessage:
    role: Literal["owner", "assistant"]
    content: str


@dataclass(frozen=True)
class ProviderKnowledge:
    subject_key: str
    content: str
    category: str
    expires_at: datetime | None


@dataclass(frozen=True)
class OwnerChatRequest:
    profile: ProviderBusinessProfile
    knowledge: tuple[ProviderKnowledge, ...]
    messages: tuple[ProviderMessage, ...]
    requested_at: datetime


@dataclass(frozen=True)
class ProposedKnowledge:
    subject_key: str
    content: str
    kind: str
    category: str
    expires_at: datetime | None = None


@dataclass(frozen=True)
class OwnerChatResult:
    reply: str
    proposed_knowledge: tuple[ProposedKnowledge, ...] = ()


class OwnerChatProvider(Protocol):
    """Replaceable provider boundary used by owner-chat orchestration."""

    def generate(self, request: OwnerChatRequest) -> OwnerChatResult: ...


class DeterministicMockOwnerChatProvider:
    """A small offline provider for development and repeatable tests."""

    def __init__(
        self,
        behavior: Literal["success", "timeout", "unavailable", "invalid"] = "success",
    ) -> None:
        self.behavior = behavior

    def generate(self, request: OwnerChatRequest) -> OwnerChatResult:
        if self.behavior == "timeout":
            raise OwnerChatProviderTimeout
        if self.behavior == "unavailable":
            raise OwnerChatProviderUnavailable
        if self.behavior == "invalid":
            raise OwnerChatProviderInvalidResponse

        owner_text = request.messages[-1].content.strip()
        facts = self._extract_facts(owner_text, request)
        if self._needs_expiry_clarification(owner_text, facts):
            reply = (
                "Please clarify exactly when that temporary information expires "
                "so I can save it safely."
            )
        elif facts:
            reply = "I saved the reusable business information from your message."
        else:
            reply = "I received your message and kept it in this owner conversation."
        return OwnerChatResult(reply=reply, proposed_knowledge=tuple(facts))

    @staticmethod
    def _needs_expiry_clarification(text: str, facts: list[ProposedKnowledge]) -> bool:
        lower = text.casefold()
        temporary_words = ("temporary", "for now", "this offer", "close early")
        return any(word in lower for word in temporary_words) and not facts

    def _extract_facts(
        self, text: str, request: OwnerChatRequest
    ) -> list[ProposedKnowledge]:
        clean = " ".join(text.split())
        lower = clean.casefold()
        operational = (
            "current stock",
            "revenue",
            "orders",
            "sales total",
            "best-selling",
            "best selling",
            "restock",
            "appointment availability",
        )
        if any(term in lower for term in operational):
            return [
                ProposedKnowledge(
                    subject_key="live_operational_data",
                    content=clean,
                    kind="permanent",
                    category="live_operational",
                )
            ]

        patterns = (
            (r"delivery charge (?:is|=)\s*(.+)", "delivery_charge", "delivery"),
            (r"return policy (?:is|=)\s*(.+)", "return_policy", "returns"),
            (r"warranty policy (?:is|=)\s*(.+)", "warranty_policy", "warranty"),
            (r"service information (?:is|=)\s*(.+)", "service_information", "service"),
        )
        for pattern, subject, category in patterns:
            match = re.search(pattern, clean, flags=re.IGNORECASE)
            if match:
                return [
                    ProposedKnowledge(
                        subject_key=subject,
                        content=match.group(1).strip().rstrip("."),
                        kind="permanent",
                        category=category,
                    )
                ]

        if "today" in lower and ("close" in lower or "closed" in lower):
            expiry = self._end_of_local_day(
                request.requested_at, request.profile.timezone
            )
            return [
                ProposedKnowledge(
                    subject_key="closing_notice",
                    content=clean,
                    kind="temporary",
                    category="temporary_notice",
                    expires_at=expiry,
                )
            ]
        return []

    @staticmethod
    def _end_of_local_day(moment: datetime, timezone_name: str) -> datetime:
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            timezone = ZoneInfo("Asia/Beirut")
        local_date = moment.astimezone(timezone).date()
        next_midnight = datetime.combine(
            local_date + timedelta(days=1), time.min, tzinfo=timezone
        )
        return (next_midnight - timedelta(microseconds=1)).astimezone(UTC)


def get_owner_chat_provider() -> OwnerChatProvider:
    """Return the Milestone 5 offline provider."""
    return DeterministicMockOwnerChatProvider()
