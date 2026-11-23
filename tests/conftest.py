from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.context import ContextPolicy
from app.main import create_app
from app.schemas.auth import UserProfile
from app.services.auth import AuthService

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE = "postgresql+psycopg://test:test@127.0.0.1:1/traceguard_test"
TEST_BROKER = "amqp://test:test@127.0.0.1:1/traceguard"


@pytest.fixture(autouse=True)
def isolated_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: pytest.FixtureRequest
):
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    for name in ContextPolicy.model_fields:
        monkeypatch.delenv(f"ANALYSIS_CONTEXT__{name.upper()}", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE)
    monkeypatch.setenv("RABBITMQ_URL", TEST_BROKER)
    get_settings.cache_clear()
    # Existing phase tests isolate their domain from authentication. Auth-marked tests
    # exercise the actual cookie/CSRF boundary, including unauthenticated API requests.
    if request.node.get_closest_marker("auth") is None:
        profile = UserProfile(
            id=UUID(int=1),
            github_id=1,
            username="phase-test",
            display_name=None,
            avatar_url=None,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            last_login_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        monkeypatch.setattr(AuthService, "authenticate", lambda *args: profile)
        monkeypatch.setattr("app.api.dependencies.check_csrf", lambda request: None)
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    return Settings(app_env="test", dependency_timeout_seconds=1)


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI):
    with TestClient(app) as client:
        yield client
