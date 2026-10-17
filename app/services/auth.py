"""Single-use browser-bound OAuth state and opaque, revocable PostgreSQL sessions."""

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import OperationalError, TimeoutError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.exceptions import (
    AuthConfigurationError,
    AuthenticationRequiredError,
    DependencyUnavailableError,
    OAuthStateError,
    UnsafeRedirectError,
)
from app.db.models.auth import AuthSession, OAuthFlow, User
from app.integrations.github import GitHubIdentityProvider
from app.schemas.auth import UserProfile

OAUTH_TTL_SECONDS = 600
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def token_digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def safe_next(value: str) -> str:
    if (
        not value.startswith("/")
        or value.startswith("//")
        or len(value) > 1024
        or any(c in value for c in ("\\", "%"))
        or any(ord(c) < 33 or ord(c) > 126 for c in value)
    ):
        raise UnsafeRedirectError
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        raise UnsafeRedirectError
    return value


@dataclass(frozen=True)
class LoginStart:
    url: str
    browser_token: str = field(repr=False)


@dataclass(frozen=True)
class LoginResult:
    user: UserProfile
    next_path: str
    token: str = field(repr=False)
    expires_at: datetime


class AuthService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        settings: Settings,
        github: GitHubIdentityProvider,
    ) -> None:
        self.sessions = sessions
        self.settings = settings
        self.github = github

    @staticmethod
    def _now(session: Session) -> datetime:
        now = session.scalar(select(func.clock_timestamp()))
        assert isinstance(now, datetime)
        return now.astimezone(UTC)

    def start(self, next_path: str) -> LoginStart:
        next_path = safe_next(next_path)
        if not self.settings.github_client_id or not self.settings.github_client_secret:
            raise AuthConfigurationError
        state, browser, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        with self.sessions.begin() as session:
            now = self._now(session)
            session.execute(delete(OAuthFlow).where(OAuthFlow.expires_at <= now))
            session.execute(delete(AuthSession).where(AuthSession.expires_at <= now))
            session.add(
                OAuthFlow(
                    state_sha256=token_digest(state),
                    browser_sha256=token_digest(browser),
                    code_verifier=verifier,
                    next_path=next_path,
                    created_at=now,
                    expires_at=now + timedelta(seconds=OAUTH_TTL_SECONDS),
                )
            )
        # Empty scope requests only public identity; no email, repository, or organization access.
        url = "https://github.com/login/oauth/authorize?" + urlencode(
            {
                "client_id": self.settings.github_client_id,
                "redirect_uri": self.settings.github_callback_url,
                "scope": "",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return LoginStart(url, browser)

    def finish(
        self,
        state: str | None,
        browser: str | None,
        code: str | None,
        *,
        old_session: str | None = None,
    ) -> LoginResult:
        if (
            state is None
            or browser is None
            or not TOKEN_PATTERN.fullmatch(state)
            or not TOKEN_PATTERN.fullmatch(browser)
        ):
            raise OAuthStateError
        with self.sessions.begin() as session:
            now = self._now(session)
            flow = session.execute(
                delete(OAuthFlow)
                .where(
                    OAuthFlow.state_sha256 == token_digest(state),
                    OAuthFlow.browser_sha256 == token_digest(browser),
                    OAuthFlow.expires_at > now,
                )
                .returning(OAuthFlow.code_verifier, OAuthFlow.next_path)
            ).one_or_none()
        # The state is consumed before external I/O. Failed exchanges cannot be replayed.
        if flow is None or not code or len(code) > 512:
            raise OAuthStateError
        profile = self.github.identity(code, flow.code_verifier)
        token = secrets.token_urlsafe(32)
        with self.sessions.begin() as session:
            now = self._now(session)
            expires = now + timedelta(seconds=self.settings.session_ttl_seconds)
            values = {
                "github_id": profile.id,
                "username": profile.login,
                "display_name": profile.name,
                "avatar_url": profile.avatar_url,
                "last_login_at": now,
            }
            statement = (
                insert(User)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[User.github_id],
                    set_={k: v for k, v in values.items() if k != "github_id"},
                )
                .returning(User)
            )
            user = session.scalars(statement).one()
            if old_session and TOKEN_PATTERN.fullmatch(old_session):
                session.execute(
                    delete(AuthSession).where(AuthSession.token_sha256 == token_digest(old_session))
                )
            session.add(
                AuthSession(
                    token_sha256=token_digest(token),
                    user_id=user.id,
                    created_at=now,
                    expires_at=expires,
                )
            )
            result = UserProfile.model_validate(user)
        return LoginResult(result, safe_next(flow.next_path), token, expires)

    def authenticate(self, token: str | None) -> UserProfile:
        if token is None or not TOKEN_PATTERN.fullmatch(token):
            raise AuthenticationRequiredError
        try:
            with self.sessions() as session:
                user = session.scalar(
                    select(User)
                    .join(AuthSession, AuthSession.user_id == User.id)
                    .where(
                        AuthSession.token_sha256 == token_digest(token),
                        AuthSession.expires_at > func.clock_timestamp(),
                    )
                )
                if user is None:
                    raise AuthenticationRequiredError
                return UserProfile.model_validate(user)
        except (OperationalError, TimeoutError) as exc:
            raise DependencyUnavailableError from exc

    def logout(self, token: str | None) -> None:
        if token is not None and TOKEN_PATTERN.fullmatch(token):
            with self.sessions.begin() as session:
                session.execute(
                    delete(AuthSession).where(AuthSession.token_sha256 == token_digest(token))
                )
