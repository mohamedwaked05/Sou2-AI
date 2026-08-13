"""Provider-neutral owner-chat generation contract and deterministic mock."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Annotated, Any, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import Depends
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


class OwnerChatProviderError(Exception):
    """Base class for safe provider failures."""


class OwnerChatProviderTimeout(OwnerChatProviderError):
    """The provider did not return within its configured deadline."""


class OwnerChatProviderUnavailable(OwnerChatProviderError):
    """The provider is temporarily unavailable."""


class OwnerChatProviderInvalidResponse(OwnerChatProviderError):
    """The provider returned an unusable response."""


@dataclass(frozen=True)
class ProviderWorkingShift:
    start: time
    end: time


@dataclass(frozen=True)
class ProviderWorkingDay:
    weekday: Literal[
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]
    is_open: bool
    shifts: tuple[ProviderWorkingShift, ...] = ()


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
    working_hours: tuple[ProviderWorkingDay, ...]


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


class _OllamaProposedKnowledge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_key: str
    content: str
    kind: Literal["permanent", "temporary"]
    category: Literal[
        "delivery",
        "returns",
        "warranty",
        "service",
        "policy",
        "temporary_notice",
        "promotion",
    ]
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def validate_expiry_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("Fact expiry must include a timezone.")
        return value

    @model_validator(mode="after")
    def validate_lifecycle(self) -> _OllamaProposedKnowledge:
        if self.kind == "permanent" and self.expires_at is not None:
            raise ValueError("Permanent facts cannot expire.")
        if self.kind == "temporary" and self.expires_at is None:
            raise ValueError("Temporary facts require an expiry.")
        return self


class _OllamaStructuredResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply: str
    proposed_knowledge: list[_OllamaProposedKnowledge]

    @field_validator("reply")
    @classmethod
    def validate_reply(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("Reply cannot be empty.")
        metadata_values = {
            "delivery",
            "returns",
            "warranty",
            "service",
            "policy",
            "temporary_notice",
            "promotion",
        }
        if clean.casefold() in metadata_values:
            raise ValueError("Reply cannot be a knowledge category value.")
        return value


class _OllamaMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Literal["assistant"]
    content: str


class _OllamaChatResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: _OllamaMessage


def _profile_context(profile: ProviderBusinessProfile) -> dict[str, Any]:
    return {
        "name": profile.name,
        "description": profile.description,
        "category": profile.category,
        "governorate": profile.governorate,
        "district": profile.district,
        "city": profile.city,
        "address_line": profile.address_line,
        "timezone": profile.timezone,
        "working_hours": [
            {
                "weekday": day.weekday,
                "is_open": day.is_open,
                "shifts": [
                    {
                        "start": shift.start.strftime("%H:%M"),
                        "end": shift.end.strftime("%H:%M"),
                    }
                    for shift in day.shifts
                ],
            }
            for day in profile.working_hours
        ],
    }


def _knowledge_context(
    knowledge: tuple[ProviderKnowledge, ...],
) -> list[dict[str, Any]]:
    return [
        {
            "subject_key": fact.subject_key,
            "content": fact.content,
            "category": fact.category,
            "expires_at": fact.expires_at.isoformat() if fact.expires_at else None,
        }
        for fact in knowledge
    ]


class OllamaOwnerChatProvider:
    """Non-streaming local Ollama implementation of the owner-chat contract."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def generate(self, request: OwnerChatRequest) -> OwnerChatResult:
        payload = self._request_payload(request)
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = client.post("/api/chat", json=payload)
            if response.status_code >= 400:
                reason = self._http_error_reason(response)
                logger.warning("Owner chat provider failed: reason=%s", reason)
                raise OwnerChatProviderUnavailable
            envelope = _OllamaChatResponse.model_validate(response.json())
            structured = _OllamaStructuredResult.model_validate_json(
                envelope.message.content
            )
            if any(
                fact.expires_at is not None and fact.expires_at <= request.requested_at
                for fact in structured.proposed_knowledge
            ):
                raise ValueError("Temporary fact expiry must be in the future.")
        except httpx.TimeoutException:
            logger.warning("Owner chat provider failed: reason=timeout")
            raise OwnerChatProviderTimeout from None
        except httpx.RequestError:
            logger.warning("Owner chat provider failed: reason=unavailable")
            raise OwnerChatProviderUnavailable from None
        except ValueError:
            logger.warning("Owner chat provider failed: reason=invalid_response")
            raise OwnerChatProviderInvalidResponse from None

        return OwnerChatResult(
            reply=structured.reply,
            proposed_knowledge=tuple(
                ProposedKnowledge(
                    subject_key=fact.subject_key,
                    content=fact.content,
                    kind=fact.kind,
                    category=fact.category,
                    expires_at=fact.expires_at,
                )
                for fact in structured.proposed_knowledge
            ),
        )

    def _request_payload(self, request: OwnerChatRequest) -> dict[str, Any]:
        context = {
            "business_profile": _profile_context(request.profile),
            "active_business_knowledge": _knowledge_context(request.knowledge),
            "request_time_utc": request.requested_at.isoformat(),
        }
        instructions = (
            "You are the private assistant for the authenticated business owner. "
            "Return one JSON object with two distinct fields. `reply` is the natural-"
            "language answer shown directly to the owner; it must answer the owner's "
            "question in English. `proposed_knowledge` is a list of reusable facts "
            "extracted only from owner messages, or an empty list when the owner did "
            "not provide a new reusable fact. `category` is metadata that belongs only "
            "inside each proposed_knowledge item. Never put a category or other "
            "metadata value such as temporary_notice, promotion, delivery, or returns "
            "in `reply`. Answer questions using the supplied business profile, "
            "complete working hours, conversation history, and approved saved "
            "knowledge. "
            "Treat working-hours shifts as local wall-clock times in the supplied "
            "business timezone. Never invent current stock, revenue, "
            "orders, sales, best sellers, restocking quantities, appointment "
            "availability, or other live operational values. Ask the owner for "
            "clarification when information is missing or a temporary fact has no "
            "clear future expiry. Propose only reusable stable facts or temporary "
            "facts with an explicit future expiry. Allowed fact categories are "
            "delivery, returns, warranty, service, policy, temporary_notice, and "
            "promotion. Do not include reasoning. Return only JSON matching the "
            "provided schema. Trusted tenant context follows:\n"
            f"{json.dumps(context, ensure_ascii=False, separators=(',', ':'))}"
        )
        messages: list[dict[str, str]] = [{"role": "system", "content": instructions}]
        messages.extend(
            {
                "role": "user" if message.role == "owner" else "assistant",
                "content": message.content,
            }
            for message in request.messages
        )
        return {
            "model": self.model,
            "stream": False,
            "format": _OllamaStructuredResult.model_json_schema(),
            "messages": messages,
        }

    @staticmethod
    def _http_error_reason(response: httpx.Response) -> str:
        if response.status_code == 404:
            return "model_missing"
        try:
            error = response.json().get("error", "")
        except ValueError:
            return "http_error"
        if "model" in str(error).casefold() and "not found" in str(error).casefold():
            return "model_missing"
        return "http_error"


def create_owner_chat_provider(
    settings: Settings,
    *,
    transport: httpx.BaseTransport | None = None,
) -> OwnerChatProvider:
    """Create the configured provider without performing network I/O."""
    if settings.owner_chat_provider == "ollama":
        return OllamaOwnerChatProvider(
            base_url=settings.ollama_base_url,
            model=settings.ollama_chat_model,
            timeout_seconds=settings.ollama_request_timeout_seconds,
            transport=transport,
        )
    return DeterministicMockOwnerChatProvider()


def get_owner_chat_provider(
    settings: Annotated[Settings, Depends(get_settings)],
) -> OwnerChatProvider:
    """FastAPI dependency selecting the configured owner-chat provider."""
    return create_owner_chat_provider(settings)
