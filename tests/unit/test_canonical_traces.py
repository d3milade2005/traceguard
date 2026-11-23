from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.traces import CanonicalTrace, TraceStep


def test_contract_supports_framework_independent_execution_details():
    step = TraceStep(
        sequence=0,
        agent_id="agent",
        event_type="tool_call",
        input={"question": "Task"},
        output={"answer": 3},
        tool_name="calculator",
        tool_arguments={"expression": "1+2"},
        tool_result=3,
        status="completed",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        step_metadata={"external_event_id": "event-1"},
    )
    trace = CanonicalTrace(source="another_framework", task="Task", steps=(step,))
    assert CanonicalTrace.model_validate_json(trace.model_dump_json()) == trace


@pytest.mark.parametrize("sequence", [-1, True, 1.5, "0"])
def test_sequence_requires_a_nonnegative_integer(sequence):
    with pytest.raises(ValidationError):
        TraceStep(sequence=sequence, event_type="message")


@pytest.mark.parametrize("sequences", [[], [1], [0, 0], [0, 2], [1, 0]])
def test_complete_history_requires_consecutive_sequence(sequences):
    steps = tuple(TraceStep(sequence=i, event_type="message") for i in sequences)
    with pytest.raises(ValidationError):
        CanonicalTrace(source="test", task="Task", steps=steps)


def test_contract_rejects_naive_timestamps():
    with pytest.raises(ValidationError):
        TraceStep(sequence=0, event_type="message", occurred_at=datetime(2026, 1, 1))


def test_contract_rejects_unmodeled_top_level_fields():
    with pytest.raises(ValidationError):
        TraceStep(sequence=0, event_type="message", mistake_step="0")
    with pytest.raises(ValidationError):
        CanonicalTrace(
            source="test",
            task="Task",
            steps=(TraceStep(sequence=0, event_type="message"),),
            question_ID="benchmark-id",
        )


def test_step_metadata_defaults_are_independent():
    first = TraceStep(sequence=0, event_type="message")
    second = TraceStep(sequence=1, event_type="message")
    first.step_metadata["role"] = "speaker"
    assert second.step_metadata == {}
