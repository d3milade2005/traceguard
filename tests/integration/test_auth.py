from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from alembic import command
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from app.core.config import get_settings
from app.core.exceptions import OAuthStateError
from app.db.models.auth import AuthSession, OAuthFlow, User
from app.main import create_app
from app.schemas.auth import GitHubProfile
from app.services.auth import AuthService, token_digest

pytestmark = [pytest.mark.integration, pytest.mark.auth]


class GitHub:
    def __init__(self):
        self.calls = []

    def identity(self, code, verifier):
        self.calls.append((code, verifier))
        return GitHubProfile(id=42, login="developer", name="Developer", avatar_url=None)


@pytest.fixture
def auth_setup(postgres):
    _, sessions, _ = postgres
    settings = get_settings().model_copy(
        update={
            "app_env": "development",
            "github_client_id": "offline-client",
            "github_client_secret": SecretStr("offline-client-secret"),
            "cors_allowed_origins": ["http://localhost:5173"],
        }
    )
    github = GitHub()
    service = AuthService(sessions, settings, github)
    return settings, sessions, github, service


def sign_in(client, settings, next_path="/dashboard"):
    started = client.get("/api/v1/auth/github", params={"next": next_path}, follow_redirects=False)
    assert started.status_code == 302
    parameters = parse_qs(urlsplit(started.headers["location"]).query, keep_blank_values=True)
    assert parameters["scope"] == [""] and parameters["code_challenge_method"] == ["S256"]
    finished = client.get(
        "/api/v1/auth/github/callback",
        params={"state": parameters["state"][0], "code": "offline-code"},
        follow_redirects=False,
    )
    assert finished.status_code == 303
    return started, finished, parameters["state"][0]


def test_oauth_roundtrip_cookie_profile_logout_and_durable_session(auth_setup, postgres):
    settings, sessions, github, service = auth_setup
    with TestClient(create_app(settings), base_url="http://localhost:8000") as client:
        client.app.state.auth = service
        started, finished, state = sign_in(client, settings, "/runs?status=failed")
        assert finished.headers["location"] == settings.frontend_origin + "/runs?status=failed"
        assert (
            "HttpOnly" in started.headers["set-cookie"]
            and "SameSite=lax" in started.headers["set-cookie"]
        )
        assert "Secure" not in started.headers["set-cookie"]
        raw_session = client.cookies.get(settings.session_cookie_name)
        me = client.get("/api/v1/auth/me")
        assert me.status_code == 200 and me.json()["github_id"] == 42
        assert "access_token" not in me.text and raw_session not in me.text
        with sessions() as session:
            assert session.get(AuthSession, token_digest(raw_session)) is not None
            assert session.get(OAuthFlow, token_digest(state)) is None
            assert session.scalar(select(User.github_id)) == 42
        assert (
            client.get(
                "/api/v1/auth/github/callback",
                params={"state": state, "code": "again"},
                follow_redirects=False,
            ).status_code
            == 400
        )
        assert len(github.calls) == 1
        assert (
            client.post("/api/v1/auth/logout", headers={"Origin": "https://evil.test"}).status_code
            == 403
        )
        assert client.get("/api/v1/auth/me").status_code == 200
        assert (
            client.post(
                "/api/v1/auth/logout", headers={"Origin": settings.frontend_origin}
            ).status_code
            == 204
        )
        assert client.get("/api/v1/auth/me").status_code == 401
        with sessions() as session:
            assert session.get(AuthSession, token_digest(raw_session)) is None
    command.check(postgres[2])


def test_state_is_bound_to_browser_and_expiry_and_consumed_once(auth_setup):
    _, sessions, github, service = auth_setup
    login = service.start("/dashboard")
    state = parse_qs(urlsplit(login.url).query)["state"][0]
    with pytest.raises(OAuthStateError):
        service.finish(state, "x" * 43, "code")
    assert not github.calls
    with sessions.begin() as session:
        flow = session.get(OAuthFlow, token_digest(state))
        assert flow.browser_sha256 != login.browser_token
        assert flow.state_sha256 != state
        flow.created_at = datetime.now(UTC) - timedelta(hours=2)
        flow.expires_at = datetime.now(UTC) - timedelta(hours=1)
    with pytest.raises(OAuthStateError):
        service.finish(state, login.browser_token, "code")
    assert not github.calls


def test_concurrent_callback_is_single_use(auth_setup):
    _, _, github, service = auth_setup
    login = service.start("/runs")
    state = parse_qs(urlsplit(login.url).query)["state"][0]

    def complete(_):
        try:
            return service.finish(state, login.browser_token, "code").user.id
        except OAuthStateError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(complete, range(2)))
    assert sum(result is not None for result in results) == 1 and len(github.calls) == 1


def test_session_expiry_rotation_and_stable_github_identity(auth_setup):
    settings, sessions, _, service = auth_setup
    with TestClient(create_app(settings), base_url="http://localhost:8000") as client:
        client.app.state.auth = service
        sign_in(client, settings)
        first = client.cookies.get(settings.session_cookie_name)
        user_id = client.get("/api/v1/auth/me").json()["id"]
        sign_in(client, settings)
        second = client.cookies.get(settings.session_cookie_name)
        assert first != second and client.get("/api/v1/auth/me").json()["id"] == user_id
        with sessions.begin() as session:
            assert session.get(AuthSession, token_digest(first)) is None
            entry = session.get(AuthSession, token_digest(second))
            entry.created_at = datetime.now(UTC) - timedelta(hours=2)
            entry.expires_at = datetime.now(UTC) - timedelta(hours=1)
        assert client.get("/api/v1/auth/me").status_code == 401


def test_secure_cookie_outside_development(auth_setup):
    settings, _, _, service = auth_setup
    settings = settings.model_copy(update={"app_env": "test"})
    service.settings = settings
    with TestClient(create_app(settings), base_url="https://localhost:8000") as client:
        client.app.state.auth = service
        started, finished, _ = sign_in(client, settings)
        assert (
            "Secure" in started.headers["set-cookie"] and "Secure" in finished.headers["set-cookie"]
        )
        assert "__Host-traceguard_session" in finished.headers["set-cookie"]
        assert "Domain=" not in finished.headers["set-cookie"]
        assert client.get("/api/v1/auth/me").status_code == 200
