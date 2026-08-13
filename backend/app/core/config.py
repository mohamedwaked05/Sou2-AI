"""Application settings loaded from environment variables."""

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
    trust_proxy_headers: bool = False
    resend_api_key: SecretStr | None = None
    resend_sender_email: str = "onboarding@resend.dev"
    frontend_base_url: str = "http://localhost:5173"
    verification_link_path: str = "/verify-email"
    password_reset_link_path: str = "/reset-password"
    owner_chat_knowledge_context_limit: int = Field(default=100, ge=1, le=200)
    owner_chat_generation_lease_seconds: int = Field(default=150, ge=5, le=300)
    owner_chat_generation_wait_seconds: int = Field(default=30, ge=1, le=300)

    @field_validator("allowed_cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: str | list[str]) -> list[str]:
        """Accept either JSON or a comma-separated environment variable."""
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("allowed_cors_origins")
    @classmethod
    def protect_production_cors(
        cls, value: list[str], info: ValidationInfo
    ) -> list[str]:
        """Disallow wildcard origins when the application runs in production."""
        environment = info.data.get("environment", "development")
        if environment.lower() == "production" and "*" in value:
            raise ValueError("ALLOWED_CORS_ORIGINS cannot contain '*' in production.")
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
            return self
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
        return self


@lru_cache
def get_settings() -> Settings:
    """Return one cached settings object per process."""
    return Settings()
