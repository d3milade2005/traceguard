from datetime import datetime
from typing import TYPE_CHECKING, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

Variant = Literal["faulty", "corrected"]
Outcome = Literal["passed", "assertion_failed", "execution_error", "timeout", "resource_limit"]
Verdict = Literal["valid", "not_reproduced", "not_discriminating", "inconclusive"]
VALIDATOR_VERSION = "regression-validator-v1"
RUNTIME_VERSION = "regression-sandbox-v1"

if TYPE_CHECKING:
    from app.core.config import Settings


class SandboxLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    timeout_seconds: int
    command_timeout_seconds: int
    memory_mb: int
    cpus: float
    pids: int
    tmp_mb: int
    output_bytes: int
    result_bytes: int
    source_bytes: int

    @classmethod
    def from_settings(cls, settings: "Settings") -> "SandboxLimits":
        return cls(
            timeout_seconds=settings.validation_timeout_seconds,
            command_timeout_seconds=settings.validation_command_timeout_seconds,
            memory_mb=settings.validation_memory_mb,
            cpus=settings.validation_cpus,
            pids=settings.validation_pids,
            tmp_mb=settings.validation_tmp_mb,
            output_bytes=settings.validation_output_bytes,
            result_bytes=settings.validation_result_bytes,
            source_bytes=settings.generated_test_max_source_bytes,
        )


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    variant: Variant
    outcome: Outcome
    exit_code: int | None = None
    duration_ms: int = Field(ge=0)
    passed: int = Field(default=0, ge=0, le=1)
    failed: int = Field(default=0, ge=0, le=1)
    errors: int = Field(default=0, ge=0, le=1)
    timed_out: bool = False
    resource_limited: bool = False
    output_truncated: bool = False
    diagnostic: Literal[
        "PASS",
        "REGRESSION_ASSERTION_FAILED",
        "EXECUTION_ERROR",
        "TIMEOUT",
        "RESOURCE_LIMIT",
        "OUTPUT_LIMIT",
        "RESULT_INVALID",
    ]
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.outcome == "passed" and (
            self.exit_code != 0 or self.passed != 1 or self.failed or self.errors
        ):
            raise ValueError("A pass requires exactly one successful test")
        if self.outcome == "assertion_failed" and (
            self.exit_code != 1 or self.failed != 1 or self.passed or self.errors
        ):
            raise ValueError("Reproduction requires exactly one assertion failure")
        if self.timed_out != (self.outcome == "timeout"):
            raise ValueError("Timeout metadata must match outcome")
        if self.resource_limited != (self.outcome == "resource_limit"):
            raise ValueError("Resource metadata must match outcome")
        return self


class RuntimeIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    image_reference: str
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    repository_digest: str | None = None
    runtime_version: Literal["regression-sandbox-v1"] = "regression-sandbox-v1"
    bundle_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    python_version: Literal["3.12.12"] = "3.12.12"
    pytest_version: Literal["9.1.1"] = "9.1.1"
    harness_implementation: Literal[
        "controlled-calculator-v1",
        "controlled-agent-invocation-v2",
        "controlled-agent-invocation-v3",
    ] = "controlled-calculator-v1"
    limits: SandboxLimits


class ValidationAccepted(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    generated_test_id: UUID
    status: Literal["pending", "queued", "processing", "completed", "failed"]


class ValidationRecord(ValidationAccepted):
    validator_version: str
    source_digest: str
    compiler_version: str
    harness_identifier: str
    harness_version: str
    image_reference: str
    runtime_identity: RuntimeIdentity | None
    verdict: Verdict | None
    faulty_outcome: Outcome | None
    corrected_outcome: Outcome | None
    faulty_execution: ExecutionResult | None
    corrected_execution: ExecutionResult | None
    failure_code: str | None
    completed_at: datetime | None
    failed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.status == "completed":
            if self.faulty_execution is None or self.corrected_execution is None:
                raise ValueError("Completed validation requires both executions")
            if (
                self.runtime_identity is None
                or self.completed_at is None
                or self.faulty_execution.variant != "faulty"
                or self.corrected_execution.variant != "corrected"
                or self.faulty_execution.outcome != self.faulty_outcome
                or self.corrected_execution.outcome != self.corrected_outcome
                or classify(self.faulty_execution, self.corrected_execution) != self.verdict
                or self.failure_code is not None
                or self.failed_at is not None
            ):
                raise ValueError("Validation result is inconsistent")
        elif (
            self.verdict is not None
            or self.faulty_execution is not None
            or self.corrected_execution is not None
        ):
            raise ValueError("Only completed validations have execution results")
        if (self.status == "failed") != (
            self.failure_code is not None and self.failed_at is not None
        ):
            raise ValueError("Failure state is inconsistent")
        return self


def classify(faulty: ExecutionResult, corrected: ExecutionResult) -> Verdict:
    if faulty.variant != "faulty" or corrected.variant != "corrected":
        raise ValueError("Execution variants are inconsistent")
    if faulty.outcome not in {"passed", "assertion_failed"} or corrected.outcome not in {
        "passed",
        "assertion_failed",
    }:
        return "inconclusive"
    if faulty.outcome == "passed":
        return "not_reproduced"
    return "valid" if corrected.outcome == "passed" else "not_discriminating"
