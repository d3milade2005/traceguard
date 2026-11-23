from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class GitHubProfile(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    id: int = Field(strict=True, gt=0)
    login: str = Field(pattern=r"^[A-Za-z0-9-]{1,39}$")
    name: str | None = Field(default=None, max_length=200)
    avatar_url: str | None = Field(default=None, max_length=2048)

    @field_validator("avatar_url")
    @classmethod
    def safe_avatar(cls, value: str | None) -> str | None:
        if value is not None:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("Avatar must be an HTTPS URL")
        return value


class UserProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    github_id: int
    username: str
    display_name: str | None
    avatar_url: str | None
    created_at: datetime
    last_login_at: datetime
