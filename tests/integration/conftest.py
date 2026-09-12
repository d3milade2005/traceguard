from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from app.core.config import get_settings
from app.db.session import create_session_factory

ROOT = Path(__file__).resolve().parents[2]


class IntegrationSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", extra="ignore", hide_input_in_errors=True
    )
    test_database_url: SecretStr | None = None
    test_rabbitmq_url: SecretStr | None = None


@pytest.fixture
def integration_settings():
    return IntegrationSettings()


@pytest.fixture
def postgres(integration_settings, monkeypatch):
    if integration_settings.test_database_url is None:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    url = make_url(integration_settings.test_database_url.get_secret_value())
    if url.drivername != "postgresql+psycopg" or not (url.database or "").endswith("_test"):
        pytest.fail("TEST_DATABASE_URL must use psycopg and a database name ending in _test")

    # Each test owns a unique schema; never migrate or clear a user's public schema.
    schema = f"traceguard_test_{uuid4().hex}"
    admin = create_engine(url, hide_parameters=True)
    with admin.begin() as connection:
        connection.execute(CreateSchema(schema))
    scoped_url = url.update_query_dict({"options": f"-csearch_path={schema}"})
    engine = create_engine(scoped_url, hide_parameters=True)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    monkeypatch.setenv("DATABASE_URL", scoped_url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    try:
        command.upgrade(config, "head")
        yield engine, create_session_factory(engine), config
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()
        get_settings.cache_clear()
