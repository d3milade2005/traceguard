from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.attributions import ReasoningEffort


class AnalysisRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    run_id: UUID
    status: Literal["active", "completed", "review_required", "failed"]
    stage: Literal["attribution", "regression_spec", "compilation", "validation"]
    model: str
    reasoning_effort: ReasoningEffort
    attribution_id: UUID | None
    regression_spec_id: UUID | None
    generated_test_id: UUID | None
    validation_id: UUID | None
    stop_reason: str | None
    deadline_at: datetime
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime
