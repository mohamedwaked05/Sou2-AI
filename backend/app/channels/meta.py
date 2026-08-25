"""Meta WhatsApp Cloud API adapter; all Meta details stay in this module."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

import httpx
from pydantic import ValidationError

from app.channels.contracts import (
    ChannelError,
    ChannelHealthResult,
    ChannelProfile,
    DeliveryStatusEvent,
    InboundTextEvent,
    NormalizedChannelEvent,
    SendTextResult,
)


class MetaWhatsAppAdapter:
    provider_type = "meta_whatsapp"

    def __init__(
        self,
        profile: ChannelProfile,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.profile = profile
        self.transport = transport

    def validate_connection(self) -> ChannelHealthResult:
        if not all(
            (
                self.profile.access_token,
                self.profile.app_secret,
                self.profile.verify_token,
                self.profile.phone_number_id,
            )
        ):
            return ChannelHealthResult(False, "channel.profile_incomplete")
        return ChannelHealthResult(True)

    def verify_challenge_token(self, supplied_token: str) -> bool:
        return hmac.compare_digest(
            supplied_token.encode(), self.profile.verify_token.encode()
        )

    def verify_webhook_signature(self, raw_body: bytes, signature: str | None) -> bool:
        if not signature or not signature.startswith("sha256="):
            return False
        supplied = signature.removeprefix("sha256=")
        if len(supplied) != 64:
            return False
        expected = hmac.new(
            self.profile.app_secret.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(supplied, expected)

    def parse_verified_events(
        self, raw_body: bytes
    ) -> tuple[NormalizedChannelEvent, ...]:
        try:
            payload = json.loads(raw_body)
        except UnicodeDecodeError, json.JSONDecodeError:
            raise ChannelError("webhook.invalid_json") from None
        if (
            not isinstance(payload, dict)
            or payload.get("object") != "whatsapp_business_account"
        ):
            raise ChannelError("webhook.invalid_payload")
        events: list[NormalizedChannelEvent] = []
        try:
            for entry in payload.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    phone_id = str(value.get("metadata", {}).get("phone_number_id", ""))
                    for message in value.get("messages", []):
                        if message.get("type") != "text":
                            continue
                        events.append(
                            InboundTextEvent(
                                provider_event_id=str(message["id"]),
                                provider_message_id=str(message["id"]),
                                phone_number_id=phone_id,
                                customer_identity=str(message["from"]),
                                text=str(message["text"]["body"]),
                                timestamp=datetime.fromtimestamp(
                                    int(message["timestamp"]), tz=UTC
                                ),
                            )
                        )
                    for status in value.get("statuses", []):
                        normalized = str(status.get("status", ""))
                        if normalized not in {"sent", "delivered", "read", "failed"}:
                            continue
                        stamp = str(status["timestamp"])
                        provider_id = str(status["id"])
                        event_id = hashlib.sha256(
                            f"{provider_id}:{normalized}:{stamp}".encode()
                        ).hexdigest()
                        events.append(
                            DeliveryStatusEvent(
                                provider_event_id=event_id,
                                provider_message_id=provider_id,
                                phone_number_id=phone_id,
                                status=normalized,
                                failure_code=(
                                    "channel.delivery_failed"
                                    if normalized == "failed"
                                    else None
                                ),
                                timestamp=datetime.fromtimestamp(int(stamp), tz=UTC),
                            )
                        )
        except KeyError, TypeError, ValueError, ValidationError:
            raise ChannelError("webhook.invalid_payload") from None
        return tuple(events)

    def send_text(self, recipient: str, text: str) -> SendTextResult:
        if not self.profile.access_token:
            raise ChannelError("channel.configuration_unavailable", retryable=False)
        try:
            with httpx.Client(
                base_url="https://graph.facebook.com",
                timeout=self.profile.request_timeout_seconds,
                transport=self.transport,
                headers={"Authorization": f"Bearer {self.profile.access_token}"},
            ) as client:
                response = client.post(
                    f"/{self.profile.graph_api_version}/{self.profile.phone_number_id}/messages",
                    json={
                        "messaging_product": "whatsapp",
                        "recipient_type": "individual",
                        "to": recipient,
                        "type": "text",
                        "text": {"preview_url": False, "body": text},
                    },
                )
        except httpx.TimeoutException:
            raise ChannelError("channel.timeout", retryable=True) from None
        except httpx.RequestError:
            raise ChannelError("channel.transport_failure", retryable=True) from None
        if response.status_code >= 400:
            retryable = (
                response.status_code in {408, 425, 429} or response.status_code >= 500
            )
            retry_after: int | None = None
            if (
                retryable
                and (value := response.headers.get("retry-after", "")).isdigit()
            ):
                retry_after = min(3600, max(1, int(value)))
            raise ChannelError(
                "channel.transient_failure" if retryable else "channel.rejected",
                retryable=retryable,
                retry_after_seconds=retry_after,
            )
        try:
            identifier = str(response.json()["messages"][0]["id"])
        except KeyError, TypeError, ValueError, IndexError:
            raise ChannelError("channel.invalid_response", retryable=True) from None
        if not 1 <= len(identifier) <= 200:
            raise ChannelError("channel.invalid_response", retryable=True)
        return SendTextResult(provider_message_id=identifier)
