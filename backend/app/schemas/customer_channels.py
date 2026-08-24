"""Safe public schemas for WhatsApp management and customer conversations."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.database.models import (
    CustomerConversationState,
    CustomerMessageStatus,
    MessagingConnectionStatus,
)


class WhatsAppConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=2, max_length=120)
    connection_profile_key: Literal["meta_whatsapp_cloud"]

    @field_validator("display_name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return " ".join(value.split())


class AutoReplyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class WhatsAppConnectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    display_name: str
    provider_type: Literal["meta_whatsapp"]
    connection_profile_key: Literal["meta_whatsapp_cloud"]
    status: MessagingConnectionStatus
    auto_reply_enabled: bool
    last_validated_at: datetime | None
    last_successful_health_check_at: datetime | None
    failure_code: str | None
    created_at: datetime
    updated_at: datetime
    capabilities: tuple[str, ...] = ("inbound_text", "outbound_text", "delivery_status")


class CustomerConversationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    masked_customer_label: str
    state: CustomerConversationState
    last_message_at: datetime | None
    latest_message_preview: str | None = None
    created_at: datetime
    updated_at: datetime


class CustomerConversationListResponse(BaseModel):
    items: list[CustomerConversationResponse]
    next_cursor: str | None


class CustomerMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    direction: Literal["inbound", "outbound"]
    sender: Literal["customer", "ai", "owner"]
    content: str
    status: CustomerMessageStatus
    reply_to_message_id: uuid.UUID | None
    failure_code: str | None
    created_at: datetime
    updated_at: datetime


class CustomerMessageListResponse(BaseModel):
    items: list[CustomerMessageResponse]
    next_cursor: str | None


class ManualReplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=4000)
    confirmed: Literal[True]

    @field_validator("content")
    @classmethod
    def clean_content(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("Message cannot be blank.")
        return clean


class WebhookAcceptedResponse(BaseModel):
    status: Literal["accepted"] = "accepted"
    events: int = Field(ge=0, le=100)
