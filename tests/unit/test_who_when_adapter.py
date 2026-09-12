import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.core.exceptions import ApplicationError, TraceAdapterError
from app.integrations.traces.who_when import WhoWhenTraceAdapter
from app.schemas.traces import CanonicalTrace

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "who_when"
VARIANTS = ("algorithm_generated", "hand_crafted")
BENCHMARK_FIELDS = (
    "question_ID",
    "ground_truth",
    "groundtruth",
    "is_correct",
    "is_corrected",
    "mistake_agent",
    "mistake_step",
    "mistake_reason",
    "mistake_type",
    "dataset_row",
    "provenance",
)


@pytest.fixture(params=[(variant, index) for variant in VARIANTS for index in range(5)])
def sample(request):
    variant, index = request.param
    records = json.loads((FIXTURES / f"{variant}_sample.json").read_text(encoding="utf-8"))
    return variant, records[index]


def test_fixture_conversion_preserves_complete_history(sample):
    variant, record = sample
    original = deepcopy(record)
    messages = json.loads(record["history"])
    trace = WhoWhenTraceAdapter(variant=variant).adapt(record)

    assert trace.source == "who_when"
    assert trace.task == record["question"]
    assert len(trace.steps) == len(messages)
    assert [step.sequence for step in trace.steps] == list(range(len(messages)))
    assert [step.output for step in trace.steps] == [message["content"] for message in messages]
    assert [step.step_metadata["role"] for step in trace.steps] == [
        message["role"] for message in messages
    ]
    for step in trace.steps:
        assert step.event_type == "message"
        assert step.input is None
        assert step.status is None
        assert step.occurred_at is None
        assert step.tool_name is None
        assert step.tool_arguments is None
        assert step.tool_result is None
    if variant == "algorithm_generated":
        assert [step.agent_id for step in trace.steps] == [message["name"] for message in messages]
        assert all(set(step.step_metadata) == {"role"} for step in trace.steps)
    else:
        assert trace.steps[0].agent_id is None
        assert trace.steps[0].step_metadata == {"role": "human"}
        assert any(step.agent_id == "Orchestrator" for step in trace.steps)
        for step, message in zip(trace.steps, messages, strict=True):
            if message["role"] in {"WebSurfer", "FileSurfer", "Assistant", "ComputerTerminal"}:
                assert step.agent_id == message["role"]
    assert CanonicalTrace.model_validate_json(trace.model_dump_json()) == trace
    assert record == original


def test_native_and_string_histories_have_identical_results(sample):
    variant, record = sample
    adapter = WhoWhenTraceAdapter(variant=variant)
    native_record = {**record, "history": json.loads(record["history"])}
    original = deepcopy(native_record)
    assert adapter.adapt(native_record) == adapter.adapt(record)
    assert native_record == original


def test_benchmark_fields_never_affect_normalization(sample):
    variant, record = sample
    adapter = WhoWhenTraceAdapter(variant=variant)
    minimal = {"question": record["question"], "history": record["history"]}
    expected = adapter.adapt(minimal)
    assert adapter.adapt(record) == expected

    # Neither malformed labels nor misleading message extras can become trace evidence.
    poisoned = deepcopy(minimal)
    poisoned["history"] = json.loads(poisoned["history"])
    for key in BENCHMARK_FIELDS:
        poisoned[key] = {"private_label": "DO_NOT_COPY"}
        for message in poisoned["history"]:
            message[key] = "DO_NOT_COPY"
    poisoned["mistake_step"] = "-999999, not an index"
    poisoned["metadata"] = {"mistake_agent": "DO_NOT_COPY"}
    for message in poisoned["history"]:
        message["step_metadata"] = {"mistake_step": "DO_NOT_COPY"}

    actual = adapter.adapt(poisoned)
    assert actual == expected
    assert "DO_NOT_COPY" not in actual.model_dump_json()
    for step in actual.steps:
        assert not set(BENCHMARK_FIELDS).intersection(step.step_metadata)


@pytest.mark.parametrize(
    ("role", "agent", "metadata"),
    [
        ("human", None, {"role": "human"}),
        ("WebSurfer", "WebSurfer", {"role": "WebSurfer"}),
        ("ComputerTerminal", "ComputerTerminal", {"role": "ComputerTerminal"}),
        ("Orchestrator", "Orchestrator", {"role": "Orchestrator"}),
        (
            "Orchestrator (thought)",
            "Orchestrator",
            {"role": "Orchestrator (thought)", "decorator": "thought"},
        ),
        (
            "Orchestrator (termination condition)",
            "Orchestrator",
            {"role": "Orchestrator (termination condition)", "decorator": "termination condition"},
        ),
        (
            "Orchestrator (-> WebSurfer)",
            "Orchestrator",
            {
                "role": "Orchestrator (-> WebSurfer)",
                "decorator": "-> WebSurfer",
                "recipient": "WebSurfer",
            },
        ),
        (
            "Orchestrator (-> FileSurfer)",
            "Orchestrator",
            {
                "role": "Orchestrator (-> FileSurfer)",
                "decorator": "-> FileSurfer",
                "recipient": "FileSurfer",
            },
        ),
    ],
)
def test_hand_crafted_speaker_and_decorations(role, agent, metadata):
    record = {"question": "Task", "history": [{"content": "body", "role": role}]}
    step = WhoWhenTraceAdapter(variant="hand_crafted").adapt(record).steps[0]
    assert step.agent_id == agent
    assert step.step_metadata == metadata


@pytest.mark.parametrize("variant", VARIANTS)
def test_empty_and_whitespace_content_preserve_positions(variant):
    # The five-row fixtures lack empty messages, so exercise this observed dataset case explicitly.
    messages = [
        {"content": body, "name": "ActualAgent", "role": "WebSurfer"}
        for body in ("before", "", " \n\t", "after")
    ]
    record = {"question": " Task\n", "history": messages}
    trace = WhoWhenTraceAdapter(variant=variant).adapt(record)
    assert trace.task == " Task\n"
    assert [(step.sequence, step.output) for step in trace.steps] == [
        (0, "before"),
        (1, ""),
        (2, " \n\t"),
        (3, "after"),
    ]


def test_algorithm_uses_name_even_when_role_is_user():
    record = {
        "question": "Task",
        "history": [{"content": "text", "name": "Verification_Expert", "role": "user"}],
    }
    step = WhoWhenTraceAdapter(variant="algorithm_generated").adapt(record).steps[0]
    assert step.agent_id == "Verification_Expert"
    assert step.step_metadata == {"role": "user"}


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize(
    "history",
    [None, 7, False, {}, [], "not JSON", "null", "{}", "[]", '"[]"', [None], ["text"], [[]]],
)
def test_malformed_histories_raise_application_error(variant, history):
    with pytest.raises(TraceAdapterError) as failure:
        WhoWhenTraceAdapter(variant=variant).adapt({"question": "Task", "history": history})
    assert isinstance(failure.value, ApplicationError)


@pytest.mark.parametrize(
    ("variant", "field"),
    [("algorithm_generated", field) for field in ("content", "role", "name")]
    + [("hand_crafted", field) for field in ("content", "role")],
)
@pytest.mark.parametrize("bad_value", ["missing", None, 17, False, [], {}])
def test_required_message_fields_are_not_inferred(variant, field, bad_value):
    message = {"content": "body", "role": "WebSurfer", "name": "ActualAgent"}
    if bad_value == "missing":
        del message[field]
    else:
        message[field] = bad_value
    record = {"question": "Task", "history": [message], "mistake_agent": "NeverAFallback"}
    with pytest.raises(TraceAdapterError):
        WhoWhenTraceAdapter(variant=variant).adapt(record)


@pytest.mark.parametrize(
    "record",
    [
        None,
        [],
        {},
        {"question": "Task"},
        {"history": []},
        {"question": 123, "history": [{}]},
        {"question": "", "history": [{}]},
    ],
)
def test_invalid_record_shape(record):
    with pytest.raises(TraceAdapterError):
        WhoWhenTraceAdapter(variant="algorithm_generated").adapt(record)


@pytest.mark.parametrize("role", ["", "  ", "Orchestrator (-> )"])
def test_invalid_hand_crafted_roles(role):
    with pytest.raises(TraceAdapterError):
        WhoWhenTraceAdapter(variant="hand_crafted").adapt(
            {"question": "Task", "history": [{"content": "", "role": role}]}
        )


@pytest.mark.parametrize("name", ["", "  ", "a" * 201])
def test_invalid_algorithm_agent_identity(name):
    with pytest.raises(TraceAdapterError):
        WhoWhenTraceAdapter(variant="algorithm_generated").adapt(
            {"question": "Task", "history": [{"content": "", "role": "assistant", "name": name}]}
        )


def test_variant_is_explicit():
    with pytest.raises(ValueError, match="variant"):
        WhoWhenTraceAdapter(variant="unknown")


def test_source_fields_do_not_become_inferred_execution_details():
    message = {
        "content": "```python\nprint(1)\n```\nexitcode: 0\n<Image>",
        "role": "ComputerTerminal",
        "input": "not a supported source field",
        "status": "completed",
        "tool_name": "python",
        "tool_arguments": {"code": "print(1)"},
        "tool_result": 1,
        "occurred_at": "2026-01-01T00:00:00Z",
    }
    trace = WhoWhenTraceAdapter(variant="hand_crafted").adapt(
        {"question": "Task", "history": [message]}
    )
    assert len(trace.steps) == 1
    step = trace.steps[0]
    assert step.output == message["content"]
    assert step.event_type == "message"
    assert step.input is step.status is step.occurred_at is None
    assert step.tool_name is step.tool_arguments is step.tool_result is None
    assert step.step_metadata == {"role": "ComputerTerminal"}


def test_normalized_metadata_does_not_alias_or_mutate_source():
    record = {"question": "Task", "history": [{"content": "", "role": "human"}]}
    original = deepcopy(record)
    adapter = WhoWhenTraceAdapter(variant="hand_crafted")
    first = adapter.adapt(record)
    second = adapter.adapt(record)
    first.steps[0].step_metadata["role"] = "changed"
    assert second.steps[0].step_metadata == {"role": "human"}
    assert record == original
