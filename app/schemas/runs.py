from datetime import datetime
from typing import Literal
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


class RunSubmissionQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=100)


class RunAccepted(BaseModel):
    id: UUID
    status: Literal[RunStatus.QUEUED]


class RunLedgerItem(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    source: str
    status: RunStatus
    created_at: datetime
    analysis_id: UUID | None = None
    analysis_status: Literal["active", "completed", "review_required", "failed"] | None = None
    analysis_stage: (
        Literal["attribution", "regression_spec", "compilation", "validation"] | None
    ) = None
    analysis_stop_reason: str | None = None
    attribution_id: UUID | None = None
    attribution_status: str | None = None
    responsible_agent: str | None = None
    decisive_sequence: int | None = None
    confidence: Literal["low", "medium", "high"] | None = None
    regression_spec_id: UUID | None = None
    regression_disposition: Literal["synthesizable", "needs_review"] | None = None
    generated_test_id: UUID | None = None
    validation_id: UUID | None = None
    validation_verdict: (
        Literal["valid", "not_reproduced", "not_discriminating", "inconclusive"] | None
    ) = None
    needs_attention: bool = False


class RunSummary(RunLedgerItem):
    updated_at: datetime
    failure_code: str | None
    failed_at: datetime | None
    normalized_step_count: int


class RunListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=100000)
    status: RunStatus | None = None
    analysis_status: Literal["active", "completed", "review_required", "failed"] | None = None
    responsible_agent: str | None = Field(default=None, min_length=1, max_length=200)
    validation_verdict: (
        Literal["valid", "not_reproduced", "not_discriminating", "inconclusive"] | None
    ) = None
    needs_attention: bool | None = None


class RunPage(BaseModel):
    items: list[RunLedgerItem]
    total: int
    limit: int
    offset: int


class DashboardSummary(BaseModel):
    total_runs: int
    active_analyses: int
    review_required_analyses: int
    valid_regressions: int
    recent_runs: list[RunLedgerItem]
    needs_attention_runs: list[RunLedgerItem]
