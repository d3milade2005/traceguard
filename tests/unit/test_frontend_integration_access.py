import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.dependencies import get_run_service
from app.core.config import Settings
from app.core.exceptions import RunNotFoundError
from app.main import create_app
from app.schemas.traces import CanonicalTrace, TraceStep

pytestmark = pytest.mark.auth
RUN_ID = "00000000-0000-0000-0000-000000000001"


def test_dev_access_is_opt_in_and_development_only(settings):
    assert settings.dev_read_access is False
    for environment in ("test", "production"):
        with pytest.raises(ValidationError):
            Settings(
                app_env=environment,
                dev_read_access=True,
                database_url=settings.database_url,
                rabbitmq_url=settings.rabbitmq_url,
            )


def test_dev_read_access_does_not_allow_writes_or_auth_me(settings):
    app = create_app(
        settings.model_copy(update={"app_env": "development", "dev_read_access": True})
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/system").status_code == 200
        assert client.get("/api/v1/auth/me").status_code == 401
        assert client.post("/api/v1/runs?source=controlled.calculator", json={}).status_code == 401


def test_trace_reads_use_existing_service_and_preserve_sequences(settings):
    class Runs:
        def canonical_trace(self, run_id):
            return CanonicalTrace(
                source="controlled.calculator",
                task="calculator",
                steps=(
                    TraceStep(
                        sequence=0, agent_id="calculator", event_type="tool_call", input={"a": 1}
                    ),
                ),
            )

        def get(self, run_id):
            from types import SimpleNamespace

            return SimpleNamespace(original_trace=b'{"original":true}')

    app = create_app(
        settings.model_copy(update={"app_env": "development", "dev_read_access": True})
    )
    app.dependency_overrides[get_run_service] = Runs
    with TestClient(app) as client:
        trace = client.get(f"/api/v1/runs/{RUN_ID}/trace")
        assert trace.status_code == 200
        assert trace.json()["steps"][0]["sequence"] == 0
        assert trace.headers["cache-control"] == "no-store"
        original = client.get(f"/api/v1/runs/{RUN_ID}/original-trace")
        assert original.content == b'{"original":true}'
        assert original.headers["x-content-type-options"] == "nosniff"
        assert client.get("/api/v1/runs/not-a-uuid/trace").status_code == 422


def test_trace_missing_run_uses_safe_error(settings):
    class Runs:
        def canonical_trace(self, run_id):
            raise RunNotFoundError

    app = create_app(
        settings.model_copy(update={"app_env": "development", "dev_read_access": True})
    )
    app.dependency_overrides[get_run_service] = Runs
    with TestClient(app) as client:
        assert client.get(f"/api/v1/runs/{RUN_ID}/trace").status_code == 404
