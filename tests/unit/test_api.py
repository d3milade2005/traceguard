from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.api.dependencies import get_readiness_service
from app.core.exceptions import DependencyUnavailableError, RunNotFoundError
from app.core.logging import log_context
from app.main import create_app


def test_health_without_infrastructure(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert len(response.headers["x-request-id"]) == 32


def test_request_id_validation_and_isolation(client):
    assert (
        client.get("/health", headers={"X-Request-ID": "request_123"}).headers["x-request-id"]
        == "request_123"
    )
    invalid = client.get("/health", headers={"X-Request-ID": "bad id"})
    assert invalid.headers["x-request-id"] != "bad id"
    assert len(client.get("/health").headers["x-request-id"]) == 32
    assert not log_context.get()


def test_concurrent_request_contexts_do_not_leak(app):
    @app.get("/context")
    def context():
        return {"request_id": log_context.get().get("request_id")}

    with TestClient(app) as client, ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(
            executor.map(
                lambda index: client.get("/context", headers={"X-Request-ID": f"req-{index}"}),
                range(8),
            )
        )
    assert [r.json()["request_id"] for r in responses] == [f"req-{i}" for i in range(8)]


def test_readiness_success(app):
    class Ready:
        def check(self):
            return None

    app.dependency_overrides[get_readiness_service] = Ready
    with TestClient(app) as client:
        assert client.get("/ready").json() == {"status": "ready"}


def test_readiness_failure_is_safe(app):
    class Unavailable:
        def check(self):
            raise DependencyUnavailableError("password=do-not-expose")

    app.dependency_overrides[get_readiness_service] = Unavailable
    with TestClient(app) as client:
        response = client.get("/ready", headers={"X-Request-ID": "ready-1"})
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "DEPENDENCY_UNAVAILABLE",
            "message": "A required service is unavailable.",
            "request_id": "ready-1",
        }
    }


@pytest.mark.parametrize("path,status", [("/missing", 404), ("/health", 405)])
def test_framework_errors_use_consistent_envelope(client, path, status):
    response = client.post(path) if status == 405 else client.get(path)
    assert response.status_code == status
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
    if status == 405:
        assert "GET" in response.headers["allow"]


def test_application_and_unexpected_errors(app):
    @app.get("/missing-run")
    def missing_run():
        raise RunNotFoundError("private database information")

    @app.get("/crash")
    def crash():
        raise RuntimeError("secret SQL and internal path")

    with TestClient(app) as client:
        missing = client.get("/missing-run")
        crash = client.get("/crash")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "RUN_NOT_FOUND"
    assert crash.status_code == 500
    assert crash.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "secret" not in crash.text
    assert crash.headers["x-request-id"] == crash.json()["error"]["request_id"]


def test_validation_errors_do_not_echo_input(app):
    class Input(BaseModel):
        count: int

    @app.post("/validate")
    def validate(body: Input):
        return body

    with TestClient(app) as client:
        response = client.post("/validate", json={"count": "secret-value"})
    assert response.status_code == 422
    assert "secret-value" not in response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_http_exception_details_are_not_exposed(app):
    @app.get("/http-error")
    def http_error():
        raise HTTPException(401, detail="secret", headers={"WWW-Authenticate": "Bearer"})

    with TestClient(app) as client:
        response = client.get("/http-error")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "secret" not in response.text


@pytest.mark.parametrize("streamed", [False, True])
def test_body_limit(settings, streamed):
    app = create_app(settings.model_copy(update={"max_request_body_bytes": 4}))
    with TestClient(app) as client:
        content = iter([b"123", b"456"]) if streamed else b"123456"
        response = client.post("/api/v1/runs?source=who_when.algorithm_generated", content=content)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_cors_includes_error_responses_and_request_id(settings):
    app = create_app(
        settings.model_copy(update={"cors_allowed_origins": ["https://client.example"]})
    )

    @app.get("/crash")
    def crash():
        raise RuntimeError("private")

    with TestClient(app) as client:
        response = client.get("/crash", headers={"Origin": "https://client.example"})
        denied = client.get("/health", headers={"Origin": "https://unknown.example"})
    assert response.headers["access-control-allow-origin"] == "https://client.example"
    assert "X-Request-ID" in response.headers["access-control-expose-headers"]
    assert "access-control-allow-origin" not in denied.headers


def test_lifespan_disposes_engine(settings, monkeypatch):
    from app import main

    real_create = main.create_database_engine
    engines = []

    def create(config):
        engine = real_create(config)
        engines.append(engine)
        return engine

    monkeypatch.setattr(main, "create_database_engine", create)
    with TestClient(create_app(settings)):
        pool_before_shutdown = engines[0].pool
    assert engines[0].pool is not pool_before_shutdown


def test_cors_preflight_has_correlation_id(settings):
    app = create_app(
        settings.model_copy(update={"cors_allowed_origins": ["https://client.example"]})
    )
    with TestClient(app) as client:
        response = client.options(
            "/health",
            headers={
                "Origin": "https://client.example",
                "Access-Control-Request-Method": "GET",
                "X-Request-ID": "preflight-1",
            },
        )
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "preflight-1"
