"""GitHub OAuth identity exchange; access tokens never leave this boundary."""

import http.client
import json
import re
from typing import Protocol
from urllib.parse import urlencode

from pydantic import SecretStr, ValidationError

from app.core.exceptions import AuthConfigurationError, GitHubUnavailableError
from app.schemas.auth import GitHubProfile


class GitHubIdentityProvider(Protocol):
    def identity(self, code: str, verifier: str) -> GitHubProfile: ...


class GitHubOAuthClient:
    def __init__(
        self,
        client_id: str | None,
        client_secret: SecretStr | None,
        callback_url: str,
        timeout_seconds: int,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.callback_url = callback_url
        self.timeout_seconds = timeout_seconds

    def _json(
        self,
        host: str,
        path: str,
        *,
        data: bytes | None = None,
        token: str | None = None,
    ) -> object:
        # Fixed HTTPS hosts and paths; http.client never follows credential-leaking redirects.
        connection = http.client.HTTPSConnection(host, timeout=self.timeout_seconds)
        headers = {"Accept": "application/json", "User-Agent": "TraceGuard"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            connection.request(
                "POST" if data is not None else "GET", path, body=data, headers=headers
            )
            response = connection.getresponse()
            payload = response.read(65537)
            if response.status != 200 or len(payload) > 65536:
                raise GitHubUnavailableError
            return json.loads(payload.decode("utf-8"))
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise GitHubUnavailableError from exc
        finally:
            connection.close()

    def identity(self, code: str, verifier: str) -> GitHubProfile:
        if self.client_id is None or self.client_secret is None:
            raise AuthConfigurationError
        result = self._json(
            "github.com",
            "/login/oauth/access_token",
            data=urlencode(
                {
                    "client_id": self.client_id,
                    "client_secret": self.client_secret.get_secret_value(),
                    "code": code,
                    "redirect_uri": self.callback_url,
                    "code_verifier": verifier,
                }
            ).encode("ascii"),
        )
        if not isinstance(result, dict):
            raise GitHubUnavailableError
        token = result.get("access_token")
        if (
            result.get("token_type") != "bearer"
            or not isinstance(token, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,4096}", token)
        ):
            raise GitHubUnavailableError
        profile = self._json("api.github.com", "/user", token=token)
        try:
            return GitHubProfile.model_validate(profile)
        except ValidationError as exc:
            raise GitHubUnavailableError from exc
