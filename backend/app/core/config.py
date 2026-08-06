"""Application settings loaded from environment variables."""

from functools import lru_cache

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    ollama_base_url: str = "http://localhost:11434"
    ollama_chat_model: str = "qwen2.5:7b"
    ollama_embedding_model: str = "bge-m3"
    postgresql_database_url: str | None = None

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


@lru_cache
def get_settings() -> Settings:
    """Return one cached settings object per process."""
    return Settings()
