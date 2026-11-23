import json
from datetime import UTC, datetime
from urllib.parse import parse_qs
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.core.exceptions import (
    AuthenticationRequiredError,
    GitHubUnavailableError,
    UnsafeRedirectError,
)
from app.integrations.github import GitHubOAuthClient
from app.main import create_app
from app.schemas.auth import UserProfile
from app.services.auth import safe_next

pytestmark = pytest.mark.auth


def profile():
    return UserProfile(
        id=UUID(int=1),
        github_id=42,
        username="developer",
        display_name=None,
        avatar_url=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_login_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.test",
        "//evil.test",
        "/\\evil.test",
        "/%2fevil",
        "/%255cevil",
        "/\r\nevil",
        "/runs\t",
        "",
        " /runs",
    ],
)
def test_next_rejects_open_redirect_and_encoded_control_variants(value):
    with pytest.raises(UnsafeRedirectError):
        safe_next(value)


@pytest.mark.parametrize(
    "value", ["/dashboard", "/runs", "/runs/abc", "/runs?status=failed", "/system"]
)
def test_safe_relative_next(value):
    assert safe_next(value) == value


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/runs"),
        ("GET", "/api/v1/runs/00000000-0000-0000-0000-000000000001"),
        ("GET", "/api/v1/dashboard"),
        ("GET", "/api/v1/system"),
        ("GET", "/api/v1/auth/me"),
        ("POST", "/api/v1/runs?source=controlled"),
        ("POST", "/api/v1/runs/00000000-0000-0000-0000-000000000001/analysis"),
        ("GET", "/api/v1/attributions/00000000-0000-0000-0000-000000000001"),
        ("GET", "/api/v1/regression-specs/00000000-0000-0000-0000-000000000001"),
        ("GET", "/api/v1/generated-tests/00000000-0000-0000-0000-000000000001"),
        ("GET", "/api/v1/validations/00000000-0000-0000-0000-000000000001"),
    ],
)
def test_operational_apis_require_cookie_without_querying_dependencies(settings, method, path):
    with TestClient(create_app(settings)) as client:
        response = client.request(method, path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_public_health_docs_and_unconfigured_login(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/api/v1/auth/github", follow_redirects=False).status_code == 503


def test_system_allowlist_and_credentialed_cors(settings):
    class Auth:
        def authenticate(self, token):
            if token != "valid":
                raise AuthenticationRequiredError
            return profile()

    settings = settings.model_copy(
        update={
            "cors_allowed_origins": ["http://localhost:5173"],
            "openai_api_key": SecretStr("private-api-key"),
            "github_client_secret": SecretStr("private-client-secret"),
        }
    )
    with TestClient(create_app(settings), base_url="https://localhost:8000") as client:
        client.app.state.auth = Auth()
        client.cookies.set(settings.session_cookie_name, "valid")
        response = client.get("/api/v1/system", headers={"Origin": settings.frontend_origin})
        assert response.status_code == 200
        assert set(response.json()) == {
            "api_health",
            "environment",
            "model",
            "reasoning_effort",
            "attribution_prompt_version",
            "context_policy_version",
            "regression_spec_version",
            "regression_prompt_version",
            "compiler_version",
            "validator_version",
        }
        assert "private-" not in response.text and "docker.sock" not in response.text
        assert response.headers["access-control-allow-origin"] == settings.frontend_origin
        assert response.headers["access-control-allow-credentials"] == "true"
        assert response.headers["cache-control"] == "no-store"
        for origin in (None, "https://evil.test", "null"):
            headers = {} if origin is None else {"Origin": origin}
            assert (
                client.post(
                    "/api/v1/runs/00000000-0000-0000-0000-000000000001/analysis", headers=headers
                ).status_code
                == 403
            )
        denied = client.get("/api/v1/system", headers={"Origin": "https://evil.test"})
        assert "access-control-allow-origin" not in denied.headers


def test_production_auth_configuration_and_secure_host_cookie(settings):
    values = settings.model_dump() | {
        "app_env": "production",
        "frontend_origin": "https://ui.example.test",
        "github_callback_url": "https://api.example.test/api/v1/auth/github/callback",
        "cors_allowed_origins": ["https://ui.example.test"],
        "github_client_id": "client",
        "github_client_secret": SecretStr("secret"),
    }
    configured = Settings(_env_file=None, **values)
    assert configured.session_cookie_secure
    assert configured.session_cookie_name.startswith("__Host-")
    for change in (
        {"github_client_secret": None},
        {"frontend_origin": "http://ui.example.test"},
        {"cors_allowed_origins": ["*"]},
        {"github_callback_url": "https://api.example.test/wrong"},
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **(values | change))


def test_production_workers_do_not_require_oauth_but_api_startup_does():
    settings = Settings(
        _env_file=None,
        app_env="production",
        database_url="postgresql+psycopg://test:test@localhost:5432/traceguard_test",
        rabbitmq_url="amqp://test:test@localhost:5672/traceguard",
    )
    with pytest.raises(ValueError, match="production API"):
        create_app(settings)


def github_adapter(monkeypatch, replies):
    calls = []

    class Connection:
        def __init__(self, host, timeout):
            self.host = host

        def request(self, method, path, body=None, headers=None):
            calls.append((self.host, method, path, body, headers))

        def getresponse(self):
            status, body = replies.pop(0)

            class Response:
                def read(self, limit):
                    return body[:limit]

            result = Response()
            result.status = status
            return result

        def close(self):
            pass

    monkeypatch.setattr("app.integrations.github.http.client.HTTPSConnection", Connection)
    return GitHubOAuthClient(
        "client",
        SecretStr("server-secret"),
        "https://api.example.test/api/v1/auth/github/callback",
        3,
    ), calls


def test_github_exchange_uses_fixed_https_pkce_and_discards_access_token(monkeypatch):
    client, calls = github_adapter(
        monkeypatch,
        [
            (200, json.dumps({"access_token": "github_token", "token_type": "bearer"}).encode()),
            (
                200,
                json.dumps(
                    {"id": 42, "login": "developer", "name": None, "avatar_url": None}
                ).encode(),
            ),
        ],
    )
    result = client.identity("code", "verifier")
    assert result.id == 42 and "github_token" not in result.model_dump_json()
    assert calls[0][:3] == ("github.com", "POST", "/login/oauth/access_token")
    assert parse_qs(calls[0][3].decode())["code_verifier"] == ["verifier"]
    assert calls[1][:3] == ("api.github.com", "GET", "/user")
    assert calls[1][4]["Authorization"] == "Bearer github_token"


@pytest.mark.parametrize(
    "status,body",
    [
        (302, b"secret redirect"),
        (500, b"secret error"),
        (200, b"{"),
        (200, b"x" * 65537),
        (200, b'{"access_token":"bad\\nvalue","token_type":"bearer"}'),
        (200, b'{"error":"bad_code"}'),
    ],
    ids=["redirect", "server-error", "invalid-json", "oversize", "invalid-token", "oauth-error"],
)
def test_github_errors_are_safe_bounded_and_not_followed_or_retried(monkeypatch, status, body):
    client, calls = github_adapter(monkeypatch, [(status, body)])
    with pytest.raises(GitHubUnavailableError) as caught:
        client.identity("secret_code", "secret_verifier")
    assert len(calls) == 1
    assert "secret" not in str(caught.value)
