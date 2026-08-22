"""Application settings loaded from environment variables."""

import ipaddress
import re
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AUTH_EVENT_MINIMUM_RETENTION_HOURS = 2
DNS_LABEL_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def normalize_trusted_host(value: str) -> str:
    """Return one canonical configured hostname without a port."""
    host = value.strip().casefold()
    wildcard = host.startswith("*.")
    if wildcard:
        host = host[2:]
    elif host == "*":
        return host
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host.endswith(".") and ":" not in host:
        host = host[:-1]
    try:
        normalized = str(ipaddress.ip_address(host))
    except ValueError:
        if (
            not host
            or len(host) > 253
            or ":" in host
            or any(
                DNS_LABEL_PATTERN.fullmatch(label) is None for label in host.split(".")
            )
        ):
            raise ValueError("TRUSTED_HOSTS contains an invalid host value.") from None
        normalized = host
    if wildcard:
        return f"*.{normalized}"
    return normalized


def _is_loopback_host(host: str) -> bool:
    plain_host = host[2:] if host.startswith("*.") else host
    if plain_host == "localhost" or plain_host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(plain_host).is_loopback
    except ValueError:
        return False


def normalize_cors_origin(value: str) -> tuple[str, str]:
    """Validate and canonicalize one explicit HTTP(S) origin."""
    raw_origin = value.strip()
    try:
        parsed = urlsplit(raw_origin)
        port = parsed.port
    except ValueError:
        raise ValueError("ALLOWED_CORS_ORIGINS contains an invalid origin.") from None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or raw_origin == "*"
        or parsed.netloc.endswith(":")
        or (port is not None and not 1 <= port <= 65_535)
    ):
        raise ValueError("ALLOWED_CORS_ORIGINS must contain explicit HTTP(S) origins.")
    try:
        host = normalize_trusted_host(parsed.hostname)
    except ValueError:
        raise ValueError("ALLOWED_CORS_ORIGINS contains an invalid origin.") from None
    if host == "*" or host.startswith("*."):
        raise ValueError("Wildcard CORS origins are forbidden.")
    scheme = parsed.scheme.casefold()
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    host_for_url = f"[{host}]" if ":" in host else host
    normalized = f"{scheme}://{host_for_url}"
    if port is not None and not default_port:
        normalized = f"{normalized}:{port}"
    return normalized, host


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
    knowledge_upload_max_bytes: int = Field(
        default=5 * 1024 * 1024, ge=1, le=5 * 1024 * 1024
    )
    knowledge_max_pdf_pages: int = Field(default=100, ge=1, le=100)
    knowledge_max_text_characters: int = Field(default=500_000, ge=1, le=500_000)
    knowledge_max_chunks: int = Field(default=500, ge=1, le=500)
    knowledge_storage_root: str = "../data/knowledge"
    redis_url: str = "redis://127.0.0.1:6379/0"
    knowledge_queue_name: str = "knowledge"
    knowledge_worker_timeout_seconds: int = Field(default=120, ge=1, le=120)
    api_docs_enabled: bool | None = None
    hsts_enabled: bool = False
    trusted_https_termination: bool = False
    log_level: str = "INFO"
    owner_chat_provider: Literal["mock", "ollama", "gemini"] = "mock"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_chat_model: str = "qwen2.5:7b"
    gemini_chat_model: str = "gemini-3-flash-preview"
    gemini_request_timeout_seconds: int = Field(default=120, ge=1)
    gemini_api_key: SecretStr | None = None
    ollama_embedding_model: str = "bge-m3"
    embedding_provider: Literal["ollama"] = "ollama"
    embedding_model: str = "bge-m3"
    embedding_batch_size: int = Field(default=16, ge=1, le=128)
    retrieval_candidate_limit: int = Field(default=10, ge=1, le=100)
    retrieval_minimum_similarity: float = Field(default=0.50, ge=-1, le=1)
    rag_context_max_chunks: int = Field(default=6, ge=1, le=10)
    rag_context_max_tokens: int = Field(default=2500, ge=1, le=5000)
    grounded_evaluation_request_interval_seconds: float = Field(default=22, ge=0)
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
        normalized = list(dict.fromkeys(normalize_trusted_host(host) for host in value))
        if info.data.get("environment", "development").lower() == "production" and any(
            host == "*" or host.startswith("*.") for host in normalized
        ):
            raise ValueError("Wildcard TRUSTED_HOSTS are forbidden in production.")
        return normalized

    @field_validator("allowed_cors_origins")
    @classmethod
    def protect_production_cors(
        cls, value: list[str], info: ValidationInfo
    ) -> list[str]:
        """Validate explicit origins and reject production loopback endpoints."""
        environment = info.data.get("environment", "development")
        normalized_origins: list[str] = []
        origin_hosts: list[str] = []
        for origin in value:
            normalized, host = normalize_cors_origin(origin)
            normalized_origins.append(normalized)
            origin_hosts.append(host)
        if environment.lower() == "production":
            if not normalized_origins:
                raise ValueError(
                    "ALLOWED_CORS_ORIGINS must contain explicit production origins."
                )
            if any(_is_loopback_host(host) for host in origin_hosts):
                raise ValueError(
                    "Development CORS origins are forbidden in production."
                )
        return list(dict.fromkeys(normalized_origins))

    @model_validator(mode="after")
    def protect_production_authentication(self) -> Settings:
        """Reject development-only authentication settings in production."""
        if self.refresh_cookie_samesite == "none" and not self.refresh_cookie_secure:
            raise ValueError("SameSite=None requires REFRESH_COOKIE_SECURE=true.")
        timeout_seconds = (
            self.ollama_request_timeout_seconds
            if self.owner_chat_provider == "ollama"
            else self.gemini_request_timeout_seconds
        )
        if self.owner_chat_provider in {"ollama", "gemini"} and (
            self.owner_chat_generation_lease_seconds <= timeout_seconds
        ):
            raise ValueError(
                "OWNER_CHAT_GENERATION_LEASE_SECONDS must exceed "
                "the selected owner-chat provider timeout."
            )
        if self.owner_chat_provider == "gemini" and (
            self.gemini_api_key is None
            or not self.gemini_api_key.get_secret_value().strip()
        ):
            raise ValueError("GEMINI_API_KEY is required when using Gemini.")
        if self.environment.lower() != "production":
            if self.hsts_enabled:
                raise ValueError("HSTS is enabled only in production.")
            return self
        if all(_is_loopback_host(host) for host in self.trusted_hosts):
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
