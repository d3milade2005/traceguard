import pytest

from app.core.exceptions import TraceAdapterError
from app.integrations.execution.controlled_system import execute_calculator_system
from app.integrations.traces.contracts import decode_record
from app.integrations.traces.controlled import ControlledTraceAdapter


def test_actual_faulty_and_corrected_agents_produce_distinct_observations():
    faulty = execute_calculator_system("faulty")
    corrected = execute_calculator_system("corrected")
    assert [s.agent_id for s in faulty.steps] == ["coordinator", "calculator", "reporter"]
    assert faulty.steps[1].output == {"sum": 5}
    assert corrected.steps[1].output == {"sum": 4}
    assert faulty.steps[2].input == faulty.steps[1].output
    assert faulty.steps[2].output == {"answer": 5}
    assert corrected.steps[2].output == {"answer": 4}
    raw = faulty.model_dump_json(indent=2).encode()
    assert ControlledTraceAdapter().adapt(decode_record(raw)) == faulty


@pytest.mark.parametrize("change", [{"source": "who_when.hand_crafted"}, {"ground_truth": 1}])
def test_controlled_adapter_rejects_wrong_source_or_extra_fields(change):
    record = execute_calculator_system().model_dump(mode="json") | change
    with pytest.raises(TraceAdapterError):
        ControlledTraceAdapter().validate_submission(record)
