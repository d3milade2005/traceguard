"""V1 envelope captured by an external application's explicitly instrumented nodes."""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator


class FailureSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    kind: Literal["exception", "evaluator_failed", "timeout", "developer_report"]
    message: str = Field(min_length=1, max_length=10000)


class LangGraphEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    agent_name: str = Field(min_length=1, max_length=200)
    event_type: Literal["invocation", "tool_call"]
    input: JsonValue = None
    output: JsonValue = None
    tool_name: str | None = Field(default=None, min_length=1, max_length=200)
    tool_arguments: JsonValue = None
    tool_result: JsonValue = None
    status: Literal["success", "error"]
    error: dict[str, str] | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if not self.agent_name.strip():
            raise ValueError("An agent name is required")
        if (self.status == "error") != (self.error is not None):
            raise ValueError("Error details and status must agree")
        if self.event_type == "tool_call" and not self.tool_name:
            raise ValueError("Tool calls require a tool name")
        if self.event_type != "tool_call" and (
            self.tool_name is not None
            or self.tool_arguments is not None
            or self.tool_result is not None
        ):
            raise ValueError("Tool fields require a tool event")
        if self.started_at and self.finished_at and self.finished_at < self.started_at:
            raise ValueError("Event finish precedes its start")
        return self


class LangGraphTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    version: Literal["langgraph-v1"]
    execution_id: UUID
    task: str = Field(min_length=1, max_length=100000)
    failure: FailureSignal
    events: tuple[LangGraphEvent, ...] = Field(min_length=1, max_length=10000)
