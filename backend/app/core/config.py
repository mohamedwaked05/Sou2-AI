"""Application settings loaded from environment variables."""

import ipaddress
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AUTH_EVENT_MINIMUM_RETENTION_HOURS = 2


class Settings(BaseSettings):
    """Runtime configuration for the Sou2AI API."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        enable_decoding=False,
        extra="ignore",
    )

    app_name: str = "Sou2AI API"
    app_version: str = "0.1.0"
    environment: str = "development"
    debug: bool = True
    api_v1_prefix: str = "/api/v1"
    host: str = "127.0.0.1"
    port: int = 8000
    allowed_cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
    )
    trusted_proxy_cidrs: list[str] = Field(default_factory=list)
    trusted_hosts: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1"])
    max_request_body_bytes: int = Field(default=65_536, ge=1)
    api_docs_enabled: bool | None = None
    hsts_enabled: bool = False
    trusted_https_termination: bool = False
    log_level: str = "INFO"
    owner_chat_provider: Literal["mock", "ollama"] = "mock"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_chat_model: str = "qwen2.5:7b"
    ollama_embedding_model: str = "bge-m3"
    ollama_request_timeout_seconds: int = Field(default=120, ge=1)
    postgresql_database_url: str = (
        "postgresql+psycopg://sou2ai_runtime_login:sou2ai_runtime_local@"
        "127.0.0.1:5433/sou2ai_dev"
    )
    test_postgresql_database_url: str = (
        "postgresql+psycopg://sou2ai_runtime_login:sou2ai_runtime_local@"
        "127.0.0.1:5433/sou2ai_test"
    )
    postgresql_connect_timeout_seconds: int = Field(default=5, ge=1)
    tool_call_audit_retention_days: int = Field(default=90, ge=1)
    tool_call_audit_hmac_secret: SecretStr | None = None
    auth_event_retention_hours: int = Field(
        default=24,
        ge=AUTH_EVENT_MINIMUM_RETENTION_HOURS,
    )
    auth_event_cleanup_interval_minutes: int = Field(default=60, ge=1)
    access_token_secret: SecretStr = Field(
        default=SecretStr("development-only-change-this-access-token-secret"),
        min_length=32,
    )
    access_token_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    access_token_lifetime_minutes: int = Field(default=15, ge=1)
    access_token_issuer: str = "sou2ai"
    access_token_audience: str = "sou2ai-api"
    refresh_session_lifetime_days: int = Field(default=1, ge=1)
    remembered_refresh_session_lifetime_days: int = Field(default=30, ge=1)
    refresh_cookie_name: str = "sou2ai_refresh_token"
    refresh_cookie_secure: bool = False
    refresh_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    refresh_cookie_domain: str | None = None
    refresh_cookie_path: str = "/api/v1/auth"
    resend_api_key: SecretStr | None = None
    resend_sender_email: str = "onboarding@resend.dev"
    frontend_base_url: str = "http://localhost:5173"
    verification_link_path: str = "/verify-email"
    password_reset_link_path: str = "/reset-password"
    owner_chat_knowledge_context_limit: int = Field(default=100, ge=1, le=200)
    owner_chat_generation_lease_seconds: int = Field(default=150, ge=5, le=300)
    owner_chat_generation_wait_seconds: int = Field(default=30, ge=1, le=300)
    owner_chat_max_output_tokens: int = Field(default=512, ge=1, le=4096)
    security_event_cleanup_interval_minutes: int = Field(default=60, ge=1)

    @field_validator(
        "allowed_cors_origins",
        "trusted_proxy_cidrs",
        "trusted_hosts",
        mode="before",
    )
    @classmethod
    def parse_list_setting(cls, value: str | list[str]) -> list[str]:
        """Accept either JSON or a comma-separated environment variable."""
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def validate_proxy_cidrs(cls, value: list[str]) -> list[str]:
        validated: list[str] = []
        for item in value:
            try:
                validated.append(str(ipaddress.ip_network(item, strict=False)))
            except ValueError as exc:
                raise ValueError(f"Invalid trusted proxy CIDR: {item}") from exc
        return validated

    @field_validator("trusted_hosts")
    @classmethod
    def validate_trusted_hosts(
        cls, value: list[str], info: ValidationInfo
    ) -> list[str]:
        if not value:
            raise ValueError("TRUSTED_HOSTS must contain at least one host.")
        if any(
            not host or "://" in host or "/" in host or " " in host for host in value
        ):
            raise ValueError("TRUSTED_HOSTS contains an invalid host value.")
        if info.data.get("environment", "development").lower() == "production" and any(
            host == "*" or host.startswith("*.") for host in value
        ):
            raise ValueError("Wildcard TRUSTED_HOSTS are forbidden in production.")
        return value

    @field_validator("allowed_cors_origins")
    @classmethod
    def protect_production_cors(
        cls, value: list[str], info: ValidationInfo
    ) -> list[str]:
        """Disallow wildcard origins when the application runs in production."""
        environment = info.data.get("environment", "development")
        if environment.lower() == "production":
            if not value or "*" in value:
                raise ValueError(
                    "ALLOWED_CORS_ORIGINS must contain explicit production origins."
                )
            if any(
                origin.startswith("http://localhost")
                or origin.startswith("http://127.0.0.1")
                for origin in value
            ):
                raise ValueError(
                    "Development CORS origins are forbidden in production."
                )
        return value

    @model_validator(mode="after")
    def protect_production_authentication(self) -> Settings:
        """Reject development-only authentication settings in production."""
        if self.refresh_cookie_samesite == "none" and not self.refresh_cookie_secure:
            raise ValueError("SameSite=None requires REFRESH_COOKIE_SECURE=true.")
        if (
            self.owner_chat_provider == "ollama"
            and self.owner_chat_generation_lease_seconds
            <= self.ollama_request_timeout_seconds
        ):
            raise ValueError(
                "OWNER_CHAT_GENERATION_LEASE_SECONDS must exceed "
                "OLLAMA_REQUEST_TIMEOUT_SECONDS when using Ollama."
            )
        if self.environment.lower() != "production":
            if self.hsts_enabled:
                raise ValueError("HSTS is enabled only in production.")
            return self
        if all(
            host in {"localhost", "127.0.0.1", "::1"} for host in self.trusted_hosts
        ):
            raise ValueError("TRUSTED_HOSTS must include the production API domain.")
        if self.access_token_secret.get_secret_value().startswith("development-only"):
            raise ValueError("ACCESS_TOKEN_SECRET must be changed in production.")
        if not self.refresh_cookie_secure:
            raise ValueError("REFRESH_COOKIE_SECURE must be true in production.")
        if (
            self.resend_api_key is None
            or not self.resend_api_key.get_secret_value()
            or "replace" in self.resend_api_key.get_secret_value().lower()
        ):
            raise ValueError("RESEND_API_KEY is required in production.")
        if self.hsts_enabled and not self.trusted_https_termination:
            raise ValueError(
                "HSTS requires TRUSTED_HTTPS_TERMINATION=true in production."
            )
        return self

    @property
    def docs_enabled(self) -> bool:
        if self.api_docs_enabled is not None:
            return self.api_docs_enabled
        return self.environment.lower() != "production"


@lru_cache
def get_settings() -> Settings:
    """Return one cached settings object per process."""
    return Settings()
