"""Deployment-managed allowlist for messaging connection profiles."""

from __future__ import annotations

import json
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
        try:
            configured = json.loads(self.settings.whatsapp_profiles_json)
        except TypeError, ValueError, json.JSONDecodeError:
            configured = {}
        keys = configured.keys() if isinstance(configured, dict) else ()
        return tuple(dict.fromkeys(("meta_whatsapp_cloud", *keys)))

    def resolve(self, key: str, *, require_outbound: bool = True) -> ChannelProfile:
        if key not in self.keys:
            raise ChannelProfileUnavailable("unsupported_profile")
        configured: dict[str, object] = {}
        if key != "meta_whatsapp_cloud":
            try:
                value = json.loads(self.settings.whatsapp_profiles_json)
                configured = value.get(key, {}) if isinstance(value, dict) else {}
            except TypeError, ValueError, json.JSONDecodeError:
                raise ChannelProfileUnavailable("profile_not_configured") from None
            if not isinstance(configured, dict):
                raise ChannelProfileUnavailable("profile_not_configured")

        def secret(name: str, fallback: object = None) -> str:
            value = configured.get(name, fallback)
            if hasattr(value, "get_secret_value"):
                return value.get_secret_value().strip()
            return str(value or "").strip()

        inbound_fields = (
            secret("app_secret", self.settings.meta_app_secret),
            secret("verify_token", self.settings.whatsapp_webhook_verify_token),
        )
        if any(not value.strip() for value in inbound_fields):
            raise ChannelProfileUnavailable("profile_not_configured")
        phone_id = secret("phone_number_id", self.settings.whatsapp_phone_number_id)
        if not phone_id:
            raise ChannelProfileUnavailable("profile_not_configured")
        access_token = secret("access_token", self.settings.whatsapp_access_token)
        if require_outbound and (not access_token):
            raise ChannelProfileUnavailable("profile_not_configured")
        graph_version = secret(
            "graph_api_version", self.settings.whatsapp_graph_api_version
        )
        timeout = int(
            configured.get(
                "request_timeout_seconds",
                self.settings.whatsapp_request_timeout_seconds,
            )
        )
        remote_validation = configured.get(
            "remote_validation_enabled",
            self.settings.whatsapp_remote_validation_enabled,
        )
        if isinstance(remote_validation, str):
            remote_validation = remote_validation.casefold() in {"1", "true", "yes"}
        return ChannelProfile(
            key=key,
            provider_type="meta_whatsapp",
            access_token=access_token,
            app_secret=inbound_fields[0],
            verify_token=inbound_fields[1],
            phone_number_id=phone_id,
            graph_api_version=graph_version,
            request_timeout_seconds=timeout,
            remote_validation_enabled=bool(remote_validation),
        )
