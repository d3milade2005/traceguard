import json
import math
import re
from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from app.schemas.attributions import AttributionRequest, ProviderUsage, ReasoningEffort
from app.schemas.llm_diagnostics import OutputDiagnostics

Scalar = Annotated[str, Field(max_length=4096)] | bool | int | float | None
PathKey = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")]
PathIndex = Annotated[int, Field(ge=0, le=10000)]
DataPath = tuple[PathKey | PathIndex, ...]
SpecStatus = Literal["pending", "queued", "processing", "completed", "failed"]
CODE_PATTERN = re.compile(
    r"(?im)(\b(?:eval|exec|__import__|compile)\s*\(|\bimport\s+\w|"
    r"\bfrom\s+\w[\w.]*\s+import\b|\b(?:def|class)\s+\w+|"
    r"\blambda\b|\bassert\s+|\b(?:pip|npm)\s+install\b|"
    r"\b(?:rm|del|remove-item|curl|wget|bash|powershell|cmd)\s+|"
    r"\$\(|```|\b[A-Za-z_]\w*(?:\.\w+)*\s*\([^)]*\))"
)


def check_data(value: JsonValue, depth: int = 0) -> None:
    """Fail closed for executable-looking values; never interpret them as source."""
    if depth > 20:
        raise ValueError("Data nesting exceeds the supported bound")
    if isinstance(value, str) and CODE_PATTERN.search(value):
        raise ValueError("Executable-looking content is unsupported")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Values must be finite JSON numbers")
    if isinstance(value, dict):
        for key, child in value.items():
            check_data(key, depth + 1)
            check_data(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            check_data(child, depth + 1)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class SourceReference(Contract):
    sequence: int = Field(ge=0)
    field: Literal["input", "output", "tool_arguments", "tool_result", "status"]
    path: DataPath = Field(max_length=16)


class Citation(Contract):
    sequence: int = Field(ge=0)
    field: Literal["task", "input", "output", "tool_arguments", "tool_result", "status"]
    quote: str = Field(min_length=1, max_length=1000)


class Scenario(Contract):
    """References resolve against complete canonical data, never executable setup."""

    stimulus: SourceReference
    preconditions: tuple[SourceReference, ...] = Field(max_length=16)


class Assertion(Contract):
    kind: Literal["equals", "not_equals", "contains", "not_contains", "exists", "not_exists"]
    observable: Literal["output", "tool_result", "status"]
    path: DataPath = Field(max_length=16)
    expected: Scalar
    evidence: tuple[Citation, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def valid_operand(self) -> Self:
        if self.kind in {"exists", "not_exists"} and self.expected is not None:
            raise ValueError("Existence assertions require null expected")
        if self.kind in {"contains", "not_contains"} and (
            not isinstance(self.expected, str) or not self.expected.strip()
        ):
            raise ValueError("Containment requires a nonempty string operand")
        return self


class RegressionSpec(Contract):
    """Phase 6 IR: all observable semantics are literals and fixed assertion kinds."""

    version: Literal["regression-spec-v1"]
    disposition: Literal["synthesizable", "needs_review"]
    test_name: str = Field(pattern=r"^test_[a-z][a-z0-9_]{0,79}$")
    title: str = Field(min_length=1, max_length=200)
    target_agent: str | None = Field(min_length=1, max_length=200)
    failure_summary: str = Field(min_length=1, max_length=2000)
    scenario: Scenario | None
    assertions: tuple[Assertion, ...] = Field(max_length=16)
    evidence: tuple[Citation, ...] = Field(max_length=20)
    review_reason: str | None = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_disposition(self) -> Self:
        if self.disposition == "synthesizable":
            if (
                not self.target_agent
                or self.scenario is None
                or not self.assertions
                or not self.evidence
            ):
                raise ValueError("Synthesis requires an agent, scenario, assertions, and evidence")
            if self.review_reason is not None:
                raise ValueError("Synthesis cannot have a review reason")
        elif self.scenario is not None or self.assertions or not self.review_reason:
            raise ValueError("Review requires a reason and no scenario or assertions")
        check_data(self.model_dump(mode="json"))
        for text in (self.title, self.failure_summary, self.review_reason):
            if text is not None and not text.strip():
                raise ValueError("Text cannot be blank")
        return self


class RegressionRequest(AttributionRequest):
    pass


class RegressionResult(Contract):
    spec: RegressionSpec
    usage: ProviderUsage = Field(default_factory=ProviderUsage)


class RegressionAccepted(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    attribution_id: UUID
    status: SpecStatus


class ScenarioSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stimulus: JsonValue
    preconditions: tuple[JsonValue, ...]


class RegressionRecord(RegressionAccepted):
    output_diagnostics: OutputDiagnostics | None = None
    disposition: Literal["synthesizable", "needs_review"] | None
    version: str | None
    title: str | None
    target_agent: str | None
    review_reason: str | None
    spec: RegressionSpec | None
    scenario_snapshot: ScenarioSnapshot | None
    provider: str
    model: str
    reasoning_effort: ReasoningEffort
    prompt_version: str
    response_id: str | None
    response_model: str | None
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    latency_ms: int | None
    input_sha256: str | None
    failure_code: str | None
    failed_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @field_validator("spec", mode="before")
    @classmethod
    def parse_persisted_json(cls, value: object) -> object:
        if isinstance(value, dict):
            return RegressionSpec.model_validate_json(json.dumps(value))
        return value
