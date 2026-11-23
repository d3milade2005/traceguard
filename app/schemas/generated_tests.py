from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator


class GeneratedTestRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    regression_spec_id: UUID
    version: Literal["generated-test-v1"]
    status: Literal["completed", "failed"]
    framework: Literal["pytest"]
    test_name: str | None
    compiler_version: str
    harness_identifier: str
    harness_version: str
    source_code: str | None
    source_digest: str | None
    failure_code: str | None
    failed_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.status == "completed":
            if (
                not self.test_name
                or not self.source_code
                or not self.source_digest
                or self.completed_at is None
                or self.failure_code is not None
                or self.failed_at is not None
            ):
                raise ValueError("Completed artifacts require source and completion metadata")
        elif (
            self.source_code is not None
            or self.source_digest is not None
            or self.completed_at is not None
            or not self.failure_code
            or self.failed_at is None
        ):
            raise ValueError("Failed compilation cannot contain a completed source artifact")
        return self
