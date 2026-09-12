from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.run_status import RunStatus


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source: str = Field(min_length=1, max_length=100)
    failure_signal: str = Field(min_length=1, max_length=10000)
    original_trace: bytes = Field(min_length=1, strict=True)
    trace_content_type: str = Field(default="application/json", min_length=1, max_length=100)
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


class RunRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    source: str
    status: RunStatus
    failure_signal: str
    original_trace: bytes
    trace_content_type: str
    request_id: str | None
    failure_code: str | None
    failed_at: datetime | None
    created_at: datetime
