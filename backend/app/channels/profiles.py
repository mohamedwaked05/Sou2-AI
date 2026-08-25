"""Deployment-managed allowlist for messaging connection profiles."""

from __future__ import annotations

from dataclasses import dataclass

from app.channels.contracts import ChannelProfile
from app.core.config import Settings


class ChannelProfileUnavailable(Exception):
    pass


@dataclass(frozen=True)
class ChannelProfileRegistry:
    settings: Settings

    @property
    def keys(self) -> tuple[str, ...]:
        return ("meta_whatsapp_cloud",)

    def resolve(self, key: str, *, require_outbound: bool = True) -> ChannelProfile:
        if key != "meta_whatsapp_cloud":
            raise ChannelProfileUnavailable("unsupported_profile")
        inbound_fields = (
            self.settings.meta_app_secret,
            self.settings.whatsapp_webhook_verify_token,
        )
        if any(
            value is None or not value.get_secret_value().strip()
            for value in inbound_fields
        ):
            raise ChannelProfileUnavailable("profile_not_configured")
        phone_id = (self.settings.whatsapp_phone_number_id or "").strip()
        if not phone_id:
            raise ChannelProfileUnavailable("profile_not_configured")
        access_token_field = self.settings.whatsapp_access_token
        if require_outbound and (
            access_token_field is None
            or not access_token_field.get_secret_value().strip()
        ):
            raise ChannelProfileUnavailable("profile_not_configured")
        access_token = (
            access_token_field.get_secret_value().strip()
            if access_token_field is not None
            else ""
        )
        return ChannelProfile(
            key=key,
            provider_type="meta_whatsapp",
            access_token=access_token,
            app_secret=inbound_fields[0].get_secret_value().strip(),  # type: ignore[union-attr]
            verify_token=inbound_fields[1].get_secret_value().strip(),  # type: ignore[union-attr]
            phone_number_id=phone_id,
            graph_api_version=self.settings.whatsapp_graph_api_version,
            request_timeout_seconds=self.settings.whatsapp_request_timeout_seconds,
        )
