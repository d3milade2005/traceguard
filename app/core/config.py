from functools import lru_cache
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.core.context import ContextPolicy
from app.schemas.attributions import ReasoningEffort


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
        env_nested_delimiter="__",
    )

    app_name: str = Field(default="Traceguard", min_length=1)
    app_env: Literal["development", "test", "production"] = "development"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    database_url: SecretStr
    rabbitmq_url: SecretStr
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    cors_allowed_origins: list[str] = Field(default_factory=list)
    max_request_body_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=5, ge=0, le=100)
    dependency_timeout_seconds: int = Field(default=3, ge=1, le=30)
    worker_concurrency: int = Field(default=2, ge=1, le=64)

    normalization_timeout_seconds: int = Field(default=60, ge=10, le=600)
    analysis_context: ContextPolicy = Field(default_factory=ContextPolicy)
    openai_api_key: SecretStr | None = None
    openai_model: str = Field(default="gpt-5.6-luna", min_length=1, max_length=200)
    openai_reasoning_effort: ReasoningEffort = "medium"
    openai_timeout_seconds: int = Field(default=60, ge=1, le=600)
    openai_max_output_tokens: int = Field(default=4096, ge=256, le=32768)
    regression_max_input_bytes: int = Field(default=48000, ge=1024, le=1000000)
    regression_max_scenario_bytes: int = Field(default=16000, ge=256, le=1000000)

    @field_validator("openai_api_key", mode="before")
    @classmethod
    def empty_key_is_absent(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
            valid = (
                url.drivername == "postgresql+psycopg"
                and bool(url.host and url.database and url.username and url.password)
                and (url.port is None or 1 <= url.port <= 65535)
            )
        except (ArgumentError, ValueError):
            valid = False
        if not valid:
            raise ValueError("DATABASE_URL must be a PostgreSQL psycopg URL with credentials")
        return value

    @field_validator("rabbitmq_url")
    @classmethod
    def validate_rabbitmq_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = urlsplit(value.get_secret_value())
            valid = (
                url.scheme in {"amqp", "amqps"}
                and bool(url.hostname and url.username and url.password)
                and (url.port is None or 1 <= url.port <= 65535)
                and not url.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("RABBITMQ_URL must be an AMQP URL with credentials")
        return value

    @field_validator("api_v1_prefix")
    @classmethod
    def validate_api_prefix(cls, value: str) -> str:
        if not value.startswith("/") or value.endswith("/") or "?" in value or "#" in value:
            raise ValueError("API_V1_PREFIX must start with / and have no trailing /")
        return value

    @field_validator("cors_allowed_origins")
    @classmethod
    def validate_origins(cls, values: list[str]) -> list[str]:
        for value in values:
            url = urlsplit(value)
            if (
                url.scheme not in {"http", "https"}
                or not url.netloc
                or url.username
                or url.password
                or url.path
                or url.query
                or url.fragment
                or "*" in value
            ):
                raise ValueError("CORS origins must be explicit HTTP(S) origins without paths")
        return values

    @model_validator(mode="after")
    def validate_production(self) -> Self:
        if self.app_env == "production" and self.debug:
            raise ValueError("DEBUG must be false in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # Required fields come from the environment.
