from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator


class TraceStep(BaseModel):
    """One canonical event; absent execution details remain unknown."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    sequence: int = Field(ge=0)
    agent_id: str | None = Field(default=None, min_length=1, max_length=200)
    event_type: str = Field(min_length=1, max_length=100)
    input: JsonValue = None
    output: JsonValue = None
    tool_name: str | None = Field(default=None, min_length=1, max_length=200)
    tool_arguments: JsonValue = None
    tool_result: JsonValue = None
    status: str | None = Field(default=None, min_length=1, max_length=50)
    occurred_at: AwareDatetime | None = None
    step_metadata: dict[str, JsonValue] = Field(default_factory=dict)


class CanonicalTrace(BaseModel):
    """A complete normalized history, before any context selection."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    source: str = Field(min_length=1, max_length=100)
    task: str = Field(min_length=1)
    steps: tuple[TraceStep, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_sequence(self) -> Self:
        if any(step.sequence != position for position, step in enumerate(self.steps)):
            raise ValueError("A complete canonical trace requires consecutive zero-based sequences")
        return self
