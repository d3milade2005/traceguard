"""Bounded diagnostics with no rejected values, validator messages, or reasoning."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OutputValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: str = Field(pattern=r"^[a-z_]{1,80}$")
    path: tuple[str | int, ...] = Field(max_length=16)


class OutputDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["structured-output-diagnostics-v1"] = "structured-output-diagnostics-v1"
    schema_name: Literal["AttributionDecision", "RegressionSpec"]
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_version: str = Field(pattern=r"^[a-z0-9-]{1,100}$")
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    category: Literal["schema_validation", "missing_output"]
    error_count: int = Field(ge=0)
    issues: tuple[OutputValidationIssue, ...] = Field(max_length=20)
    omitted_issue_count: int = Field(ge=0)
