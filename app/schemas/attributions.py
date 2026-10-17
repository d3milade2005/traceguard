from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from app.core.attribution_status import AttributionStatus
from app.schemas.llm_diagnostics import OutputDiagnostics

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]
Confidence = Literal["low", "medium", "high"]
Decision = Literal["attributed", "abstain"]


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    start_sequence: int = Field(ge=0)
    end_sequence: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.end_sequence < self.start_sequence or not self.reason.strip():
            raise ValueError("Evidence requires an ordered inclusive range and a reason")
        return self


class AttributionDecision(BaseModel):
    """A short diagnosis, never private reasoning; nullable fields are explicitly required."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    decision: Decision
    responsible_agent: str | None = Field(min_length=1, max_length=200)
    decisive_sequence: int | None = Field(ge=0)
    failure_category: str | None = Field(min_length=1, max_length=100)
    explanation: str = Field(min_length=1, max_length=2000)
    confidence: Confidence
    evidence: tuple[EvidenceReference, ...] = Field(max_length=20)
    abstention_reason: str | None = Field(min_length=1, max_length=1000)

    @field_validator("responsible_agent", "failure_category", "explanation", "abstention_reason")
    @classmethod
    def reject_blank_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Diagnosis text cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.decision == "attributed":
            if (
                self.responsible_agent is None
                or self.decisive_sequence is None
                or not self.evidence
            ):
                raise ValueError("Attribution requires an agent, decisive sequence, and evidence")
            if self.abstention_reason is not None:
                raise ValueError("Attributed decisions cannot have an abstention reason")
        elif (
            self.responsible_agent is not None
            or self.decisive_sequence is not None
            or self.failure_category is not None
            or self.abstention_reason is None
        ):
            raise ValueError("Abstention requires a reason and no agent, sequence, or category")
        previous = -1
        for evidence in self.evidence:
            if evidence.start_sequence <= previous:
                raise ValueError("Evidence ranges must be ordered and non-overlapping")
            previous = evidence.end_sequence
        return self


class AttributionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    prompt_version: str
    instructions: str
    evidence_json: str
    model: str
    reasoning_effort: ReasoningEffort


class ProviderUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    response_id: str | None = Field(default=None, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    response_model: str | None = Field(default=None, max_length=200)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)


class LLMResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    attribution: AttributionDecision
    usage: ProviderUsage = Field(default_factory=ProviderUsage)


class PersistedEvidence(EvidenceReference):
    model_config = ConfigDict(from_attributes=True, extra="forbid", frozen=True)

    contains_truncated: bool


class AttributionAccepted(BaseModel):
    id: UUID
    run_id: UUID
    status: AttributionStatus


class AttributionRecord(AttributionAccepted):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    decision: Decision | None
    responsible_agent: str | None
    decisive_sequence: int | None
    failure_category: str | None
    explanation: str | None
    confidence: Confidence | None
    abstention_reason: str | None
    evidence: tuple[PersistedEvidence, ...] = ()
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
    context_manifest: dict[str, JsonValue] | None
    output_diagnostics: OutputDiagnostics | None = None
    failure_code: str | None
    failed_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
