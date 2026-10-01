from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.core.context import ContextPolicy


def test_missing_critical_configuration(monkeypatch):
    monkeypatch.delenv("DATABASE_URL")
    with pytest.raises(ValidationError, match="database_url"):
        Settings()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_url", "sqlite:///database.db"),
        ("database_url", "postgresql+psycopg://user:secret@localhost:notaport/db"),
        ("rabbitmq_url", "redis://user:secret@localhost:6379/0"),
        ("api_v1_prefix", "/api/v1/"),
        ("cors_allowed_origins", ["*"]),
        ("cors_allowed_origins", ["https://example.com/path"]),
        ("db_pool_size", 0),
        ("worker_concurrency", 0),
    ],
)
def test_invalid_configuration_is_rejected(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_production_disables_debug():
    with pytest.raises(ValidationError, match="DEBUG"):
        Settings(app_env="production", debug=True)


def test_settings_repr_masks_credentials(settings):
    assert "test:test" not in repr(settings)
    assert "**********" in repr(settings)


def test_openai_defaults_and_optional_secret(monkeypatch):
    settings = Settings()
    assert settings.openai_api_key is None
    assert settings.openai_model == "gpt-5.6-luna"
    assert settings.openai_reasoning_effort == "medium"
    assert settings.openai_timeout_seconds == 60
    assert settings.openai_max_output_tokens == 4096
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-secret")
    monkeypatch.setenv("OPENAI_MODEL", "configured-model")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "low")
    configured = Settings()
    assert configured.openai_model == "configured-model"
    assert configured.openai_reasoning_effort == "low"
    assert "unit-test-secret" not in repr(configured)


@pytest.mark.parametrize(
    "options",
    [
        {"openai_model": ""},
        {"openai_reasoning_effort": "unsupported"},
        {"openai_timeout_seconds": 0},
        {"openai_max_output_tokens": 0},
    ],
)
def test_invalid_openai_configuration(options):
    with pytest.raises(ValidationError):
        Settings(**options)


def test_empty_openai_key_is_absent(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert Settings().openai_api_key is None


def test_dotenv_loading_and_environment_precedence(tmp_path: Path, monkeypatch):
    (tmp_path / ".env").write_text(
        'APP_NAME=From dotenv\nLOG_LEVEL=WARNING\nCORS_ALLOWED_ORIGINS=["http://localhost:3000"]',
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_NAME", "From environment")
    settings = Settings()
    assert settings.app_name == "From environment"
    assert settings.log_level == "WARNING"
    assert settings.cors_allowed_origins == ["http://localhost:3000"]
    assert get_settings() is get_settings()


def test_context_defaults_and_nested_environment_configuration(monkeypatch):
    assert Settings().analysis_context == ContextPolicy()
    monkeypatch.setenv("ANALYSIS_CONTEXT__BUDGET", "12000")
    monkeypatch.setenv("ANALYSIS_CONTEXT__RESERVED_HEADROOM", "1000")
    monkeypatch.setenv("ANALYSIS_CONTEXT__HEAD_STEPS", "3")
    assert Settings().analysis_context.budget == 12000
    assert Settings().analysis_context.effective_budget == 11000
    assert Settings().analysis_context.head_steps == 3


@pytest.mark.parametrize(
    "options",
    [
        {"budget": 0},
        {"reserved_headroom": -1},
        {"budget": 100, "reserved_headroom": 100},
        {"head_steps": 0},
        {"tail_steps": 0},
        {"neighbour_steps": 0},
    ],
)
def test_invalid_context_configuration_is_rejected(options):
    with pytest.raises(ValidationError):
        Settings(analysis_context=options)
