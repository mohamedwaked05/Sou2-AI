"""Stable contracts for external text messaging providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator


class InboundTextEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["message"] = "message"
    provider_event_id: str = Field(min_length=1, max_length=200)
    provider_message_id: str = Field(min_length=1, max_length=200)
    phone_number_id: str = Field(min_length=1, max_length=100)
    customer_identity: str = Field(min_length=3, max_length=64)
    text: str = Field(min_length=1, max_length=4000)
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Timestamp must include a timezone.")
        return value


class DeliveryStatusEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["status"] = "status"
    provider_event_id: str = Field(min_length=1, max_length=200)
    provider_message_id: str = Field(min_length=1, max_length=200)
    phone_number_id: str = Field(min_length=1, max_length=100)
    status: Literal["sent", "delivered", "read", "failed"]
    failure_code: str | None = Field(default=None, max_length=100)
    timestamp: datetime


NormalizedChannelEvent = InboundTextEvent | DeliveryStatusEvent


@dataclass(frozen=True)
class ChannelProfile:
    key: str
    provider_type: Literal["meta_whatsapp"]
    access_token: str
    app_secret: str
    verify_token: str
    phone_number_id: str
    graph_api_version: str
    request_timeout_seconds: int


@dataclass(frozen=True)
class ChannelHealthResult:
    healthy: bool
    failure_code: str | None = None


@dataclass(frozen=True)
class SendTextResult:
    provider_message_id: str
    status: Literal["sent"] = "sent"


class ChannelError(Exception):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code)


@runtime_checkable
class MessagingChannelAdapter(Protocol):
    provider_type: str

    def validate_connection(self) -> ChannelHealthResult: ...

    def verify_webhook_signature(
        self, raw_body: bytes, signature: str | None
    ) -> bool: ...

    def verify_challenge_token(self, supplied_token: str) -> bool: ...

    def parse_verified_events(
        self, raw_body: bytes
    ) -> tuple[NormalizedChannelEvent, ...]: ...

    def send_text(self, recipient: str, text: str) -> SendTextResult: ...
