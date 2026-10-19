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
    dev_read_access: bool = False
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
    frontend_origin: str = "http://localhost:5173"
    github_client_id: str | None = Field(
        default=None, min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.-]+$"
    )
    github_client_secret: SecretStr | None = None
    github_callback_url: str = Field(
        default="http://localhost:8000/api/v1/auth/github/callback", max_length=2048
    )
    session_ttl_seconds: int = Field(default=43200, ge=300, le=604800)

    normalization_timeout_seconds: int = Field(default=60, ge=10, le=600)
    analysis_context: ContextPolicy = Field(default_factory=ContextPolicy)
    openai_api_key: SecretStr | None = None
    openai_model: str = Field(default="gpt-5.6-luna", min_length=1, max_length=200)
    openai_reasoning_effort: ReasoningEffort = "medium"
    openai_timeout_seconds: int = Field(default=60, ge=1, le=600)
    openai_max_output_tokens: int = Field(default=4096, ge=256, le=32768)
    regression_max_input_bytes: int = Field(default=48000, ge=1024, le=1000000)
    regression_max_scenario_bytes: int = Field(default=16000, ge=256, le=1000000)
    generated_test_max_source_bytes: int = Field(default=262144, ge=1024, le=4194304)
    validation_image: str = "traceguard-validation:1"
    validation_timeout_seconds: int = Field(default=10, ge=1, le=60)
    validation_command_timeout_seconds: int = Field(default=10, ge=2, le=30)
    validation_memory_mb: int = Field(default=256, ge=64, le=1024)
    validation_cpus: float = Field(default=1.0, ge=0.1, le=2.0)
    validation_pids: int = Field(default=32, ge=16, le=128)
    validation_tmp_mb: int = Field(default=16, ge=4, le=64)
    validation_output_bytes: int = Field(default=32768, ge=1024, le=131072)
    validation_result_bytes: int = Field(default=32768, ge=1024, le=131072)

    @field_validator("validation_image")
    @classmethod
    def pinned_validation_image(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./_:@-]{0,299}", value):
            raise ValueError("Validation image reference is invalid")
        if "@sha256:" not in value and not value.startswith("sha256:"):
            tag = value.rsplit("/", 1)[-1]
            if ":" not in tag or tag.rsplit(":", 1)[-1] == "latest":
                raise ValueError(
                    "Validation image requires an explicit non-latest version or digest"
                )
        return value

    @field_validator("openai_api_key", "github_client_secret", "github_client_id", mode="before")
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

    @field_validator("frontend_origin")
    @classmethod
    def validate_frontend_origin(cls, value: str) -> str:
        cls.validate_origins([value])
        return value

    @property
    def backend_origin(self) -> str:
        parsed = urlsplit(self.github_callback_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def session_cookie_secure(self) -> bool:
        return self.app_env != "development" or urlsplit(self.github_callback_url).scheme == "https"

    @property
    def session_cookie_name(self) -> str:
        return "__Host-traceguard_session" if self.session_cookie_secure else "traceguard_session"

    @property
    def oauth_cookie_name(self) -> str:
        return "__Host-traceguard_oauth" if self.session_cookie_secure else "traceguard_oauth"

    @model_validator(mode="after")
    def validate_production(self) -> Self:
        if self.dev_read_access and self.app_env != "development":
            raise ValueError("DEV_READ_ACCESS is restricted to development")
        if self.app_env == "production" and self.debug:
            raise ValueError("DEBUG must be false in production")
        callback = urlsplit(self.github_callback_url)
        if (
            callback.scheme not in {"http", "https"}
            or not callback.hostname
            or callback.username
            or callback.password
            or callback.query
            or callback.fragment
            or callback.path != f"{self.api_v1_prefix}/auth/github/callback"
        ):
            raise ValueError("GITHUB_CALLBACK_URL must match the configured auth callback path")
        # Validate the port and origin with the same explicit-origin rules used for CORS.
        _ = callback.port
        self.validate_origins([self.backend_origin])
        if bool(self.github_client_id) != bool(self.github_client_secret):
            raise ValueError("Configure both GitHub OAuth client ID and secret")
        for origin in (self.frontend_origin, self.backend_origin):
            parsed = urlsplit(origin)
            _ = parsed.port
            if parsed.scheme != "https" and (
                self.app_env not in {"development", "test"}
                and self.github_client_id is not None
                or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            ):
                raise ValueError("HTTP auth origins are permitted only for local development/tests")
        if self.app_env == "production" and self.github_client_id is not None:
            if not self.github_client_id or not self.github_client_secret:
                raise ValueError("GitHub OAuth credentials are required in production")
            if (
                self.frontend_origin != self.backend_origin
                and self.frontend_origin not in self.cors_allowed_origins
            ):
                raise ValueError(
                    "The frontend origin must be explicitly allowed for credentialed CORS"
                )
        return self

    def validate_api_auth(self) -> None:
        # Worker settings do not require OAuth secrets. Only API startup enables login.
        if self.app_env == "production" and (
            not self.github_client_id or not self.github_client_secret
        ):
            raise ValueError("GitHub OAuth credentials are required for the production API")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # Required fields come from the environment.
