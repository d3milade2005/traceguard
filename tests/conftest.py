from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE = "postgresql+psycopg://test:test@127.0.0.1:1/traceguard_test"
TEST_BROKER = "amqp://test:test@127.0.0.1:1/traceguard"


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE)
    monkeypatch.setenv("RABBITMQ_URL", TEST_BROKER)
    get_settings.cache_clear()
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
