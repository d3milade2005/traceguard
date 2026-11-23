from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.traces import TraceStep


class ContextContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class SequenceRange(ContextContract):
    """Inclusive canonical sequence bounds, never positions in the selected tuple."""

    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.end < self.start:
            raise ValueError("Sequence range end must be at least its start")
        return self


class TruncatedTraceStep(ContextContract):
    """Beginning/end fragments of canonical step JSON; explicitly incomplete evidence."""

    representation: Literal["serialized_excerpt"] = "serialized_excerpt"
    sequence: int = Field(ge=0)
    agent_id: str | None = Field(default=None, min_length=1, max_length=200)
    event_type: str = Field(min_length=1, max_length=100)
    prefix: str
    suffix: str
    estimated_original_size: int = Field(ge=1)
    estimated_selected_size: int = Field(ge=1)


class ContextStatistics(ContextContract):
    size_unit: str = Field(min_length=1)
    budget: int = Field(ge=1)
    reserved_headroom: int = Field(ge=0)
    effective_budget: int = Field(ge=1)
    original_step_count: int = Field(ge=1)
    selected_step_count: int = Field(ge=1)
    estimated_original_size: int = Field(ge=1)
    estimated_selected_size: int = Field(ge=1)
    pruning_occurred: bool


class AnalysisContext(ContextContract):
    """Derived evidence only; full steps and excerpts retain canonical sequences."""

    run_id: UUID | None = None
    source: str = Field(min_length=1, max_length=100)
    task: str = Field(min_length=1)
    steps: tuple[TraceStep | TruncatedTraceStep, ...] = Field(min_length=1)
    full_trace_used: bool
    omitted_ranges: tuple[SequenceRange, ...] = ()
    statistics: ContextStatistics

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        sequences = [step.sequence for step in self.steps]
        count = self.statistics.original_step_count
        if sequences != sorted(set(sequences)) or sequences[-1] >= count:
            raise ValueError("Selected steps must have ordered, unique original sequences")
        if self.statistics.selected_step_count != len(self.steps):
            raise ValueError("Selected step count must match the evidence")
        expected: list[SequenceRange] = []
        previous = -1
        for sequence in [*sequences, count]:
            if sequence > previous + 1:
                expected.append(SequenceRange(start=previous + 1, end=sequence - 1))
            previous = sequence
        if tuple(expected) != self.omitted_ranges:
            raise ValueError("Omitted ranges must describe exactly the unselected sequences")
        complete = not expected and all(isinstance(step, TraceStep) for step in self.steps)
        if self.full_trace_used != complete or self.statistics.pruning_occurred == complete:
            raise ValueError("Full-trace and pruning flags must match the evidence")
        stats = self.statistics
        if stats.effective_budget != stats.budget - stats.reserved_headroom:
            raise ValueError("Effective budget must account for reserved headroom")
        # Candidate contexts can exceed the budget while selection is in progress.
        # The service checks the final serialized size before returning a result.
        return self
