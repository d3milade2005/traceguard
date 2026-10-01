import json
import socket
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.context import ContextPolicy
from app.core.exceptions import ContextBudgetError
from app.integrations.traces.who_when import WhoWhenTraceAdapter
from app.schemas.context import AnalysisContext, SequenceRange, TruncatedTraceStep
from app.schemas.traces import CanonicalTrace, TraceStep
from app.services.context import ContextManager, UTF8SizeEstimator

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "who_when"


def trace_of(count=10, content_size=100, *, agents=None):
    return CanonicalTrace(
        source="independent_framework",
        task="Original task",
        steps=tuple(
            TraceStep(
                sequence=i,
                agent_id=agents[i] if agents else "Agent",
                event_type="message",
                output=f"step {i}:" + "x" * content_size,
                step_metadata={"role": "assistant"},
            )
            for i in range(count)
        ),
    )


def manager(budget=65536, **options):
    return ContextManager(ContextPolicy(budget=budget, reserved_headroom=0, **options))


def assert_bounded(context):
    stats = context.statistics
    assert stats.estimated_selected_size == len(context.model_dump_json().encode("utf-8"))
    assert stats.estimated_selected_size <= stats.effective_budget
    assert stats.effective_budget == stats.budget - stats.reserved_headroom
    assert stats.selected_step_count == len(context.steps)
    assert AnalysisContext.model_validate_json(context.model_dump_json()) == context


def test_full_trace_is_preferred_and_preserves_every_sequence():
    trace = trace_of()
    run_id = uuid4()
    context = manager().build(trace, run_id=run_id)
    assert context.steps == trace.steps
    assert [s.sequence for s in context.steps] == list(range(10))
    assert context.source == trace.source
    assert context.task == trace.task
    assert context.run_id == run_id
    assert context.full_trace_used
    assert not context.omitted_ranges
    assert not context.statistics.pruning_occurred
    assert context.statistics.original_step_count == 10
    assert context.statistics.estimated_original_size == context.statistics.estimated_selected_size
    assert_bounded(context)


def test_long_trace_keeps_endpoint_and_transition_windows_without_renumbering():
    trace = trace_of(40, 60, agents=["A"] * 20 + ["B"] * 20)
    service = manager(4300, head_steps=2, tail_steps=2, neighbour_steps=1)
    context = service.build(trace)
    assert [s.sequence for s in context.steps] == [0, 1, 18, 19, 20, 21, 38, 39]
    assert context.omitted_ranges == (
        SequenceRange(start=2, end=17),
        SequenceRange(start=22, end=37),
    )
    assert context.statistics.pruning_occurred
    assert not context.full_trace_used
    assert context.statistics.original_step_count == 40
    assert context.statistics.estimated_original_size > context.statistics.effective_budget
    assert context == service.build(trace)
    assert context.steps == tuple(trace.steps[i] for i in [0, 1, 18, 19, 20, 21, 38, 39])
    assert_bounded(context)


def test_transition_windows_are_selected_recent_first_and_atomically():
    agents = ["A"] * 8 + ["B"] * 12 + ["C"] * 20
    context = manager(3000, head_steps=1, tail_steps=1).build(trace_of(40, 60, agents=agents))
    sequences = {s.sequence for s in context.steps}
    assert {18, 19, 20, 21} <= sequences
    assert not sequences & {6, 7, 8, 9}
    assert_bounded(context)


def test_no_agent_transition_is_invented_for_unknown_human_speakers():
    context = manager(2500, head_steps=2, tail_steps=2).build(trace_of(30, 150, agents=[None] * 30))
    assert [s.sequence for s in context.steps] == [0, 1, 28, 29]
    assert_bounded(context)


def test_budget_counts_unicode_json_escaping_omissions_and_statistics():
    trace = trace_of(20, 100)
    trace = trace.model_copy(
        update={
            "task": 'Task with quotes " and unicode 🌍',
            "steps": tuple(s.model_copy(update={"output": '🌍\n"\\' * 100}) for s in trace.steps),
        }
    )
    service = ContextManager(ContextPolicy(budget=3200, reserved_headroom=500))
    context = service.build(trace)
    assert_bounded(context)
    assert context.statistics.size_unit == "utf8_bytes"
    assert context.statistics.effective_budget == 2700
    assert UTF8SizeEstimator().estimate("🌍") == 4


def test_single_oversized_step_is_an_explicit_beginning_end_excerpt():
    trace = CanonicalTrace(
        source="framework",
        task="Task",
        steps=(
            TraceStep(
                sequence=0,
                agent_id="A",
                event_type="message",
                output="BEGIN" + "x" * 100000 + "END",
            ),
        ),
    )
    before = trace.model_dump_json()
    context = manager(2400).build(trace)
    step = context.steps[0]
    assert isinstance(step, TruncatedTraceStep)
    assert step.sequence == 0
    assert step.agent_id == "A"
    assert step.event_type == "message"
    serialized = trace.steps[0].model_dump_json()
    assert step.prefix and serialized.startswith(step.prefix)
    assert step.suffix and serialized.endswith(step.suffix)
    assert "BEGIN" in step.prefix and "END" in step.suffix
    assert step.estimated_original_size == len(serialized.encode("utf-8"))
    assert step.estimated_selected_size == len(step.model_dump_json().encode("utf-8"))
    assert step.estimated_selected_size < step.estimated_original_size
    assert trace.model_dump_json() == before
    assert not context.full_trace_used
    assert context.statistics.pruning_occurred
    assert context.omitted_ranges == ()
    assert_bounded(context)


def test_two_oversized_endpoints_share_budget_and_retain_original_references():
    trace = trace_of(5, 100000)
    context = manager(3600).build(trace)
    assert [s.sequence for s in context.steps] == [0, 4]
    for step in context.steps:
        assert isinstance(step, TruncatedTraceStep)
        assert step.prefix and step.suffix
    assert context.omitted_ranges == (SequenceRange(start=1, end=3),)
    assert_bounded(context)


def test_large_structured_payloads_are_fragments_not_fabricated_canonical_values():
    step = TraceStep(
        sequence=0,
        event_type="tool_result",
        input={"huge": ["a" * 30000]},
        output={"nested": {"huge": "b" * 30000}},
        tool_arguments={"huge": "c" * 30000},
        tool_result=["d" * 30000],
        step_metadata={"huge": "e" * 30000},
    )
    trace = CanonicalTrace(source="custom", task="Task", steps=(step,))
    before = deepcopy(trace.model_dump())
    context = manager(2000).build(trace)
    assert isinstance(context.steps[0], TruncatedTraceStep)
    assert trace.model_dump() == before
    assert_bounded(context)


@pytest.mark.parametrize("budget", [1, 300, 1000])
def test_impossible_task_or_envelope_budget_fails_explicitly(budget):
    trace = trace_of(1).model_copy(update={"task": "task" * 3000})
    before = trace.model_dump_json()
    with pytest.raises(ContextBudgetError):
        manager(budget).build(trace)
    assert trace.model_dump_json() == before


def test_empty_messages_humans_decorated_agents_and_execution_fields_remain_valid():
    trace = CanonicalTrace(
        source="custom",
        task="Task",
        steps=(
            TraceStep(sequence=0, event_type="message", output="", step_metadata={"role": "human"}),
            TraceStep(
                sequence=1,
                agent_id="Orchestrator (-> Worker)",
                event_type="tool_call",
                input={"items": [1, 2]},
                output="",
                tool_name="lookup",
                tool_arguments={"q": "Task"},
                tool_result=[],
                status="completed",
                occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
                step_metadata={"recipient": "Worker", "nested": {"roles": ["A"]}},
            ),
        ),
    )
    context = manager().build(trace)
    assert context.steps == trace.steps
    assert context.steps[1].agent_id == "Orchestrator (-> Worker)"
    assert_bounded(context)
    before = trace.model_dump_json()
    context.steps[1].input["items"].append(3)
    context.steps[1].step_metadata["nested"]["roles"].append("B")
    assert trace.model_dump_json() == before


@pytest.mark.parametrize("budget", [65536, 1800])
def test_selection_and_returned_json_are_detached_from_persisted_style_input(budget):
    trace = trace_of(10, 200)
    persisted_style = trace.model_dump()
    before = deepcopy(persisted_style)
    reconstructed = CanonicalTrace.model_validate(persisted_style)
    context = manager(budget).build(reconstructed)
    assert reconstructed.model_dump() == before
    assert persisted_style == before
    for step in context.steps:
        if isinstance(step, TraceStep):
            step.step_metadata["role"] = "changed by consumer"
    assert reconstructed.model_dump() == before
    assert persisted_style == before


@pytest.mark.parametrize("variant", ["algorithm_generated", "hand_crafted"])
def test_committed_who_when_fixtures_remain_label_free_and_non_destructive(variant):
    rows = json.loads((FIXTURES / f"{variant}_sample.json").read_text(encoding="utf-8"))
    adapter = WhoWhenTraceAdapter(variant=variant)
    service = manager(8000)
    forbidden = {
        "mistake_agent",
        "mistake_step",
        "mistake_reason",
        "mistake_type",
        "ground_truth",
        "groundtruth",
        "question_ID",
        "is_correct",
        "is_corrected",
    }
    for row in rows:
        before = deepcopy(row)
        trace = adapter.adapt(row)
        original = trace.model_dump_json()
        context = service.build(trace)
        assert trace.model_dump_json() == original
        assert row == before
        assert_bounded(context)
        changed_labels = {**row, **{key: "sentinel-evaluation-only" for key in forbidden}}
        assert service.build(adapter.adapt(changed_labels)) == context
        assert "sentinel-evaluation-only" not in context.model_dump_json()
        assert forbidden.isdisjoint(context.model_dump())
        for step in context.steps:
            if isinstance(step, TraceStep):
                assert step == trace.steps[step.sequence]
                assert forbidden.isdisjoint(step.step_metadata)


def test_estimator_can_be_replaced_without_provider_or_framework_code():
    class CharacterEstimator:
        unit = "characters"

        def estimate(self, serialized: str) -> int:
            return len(serialized)

    trace = trace_of(30, 100)
    context = ContextManager(
        ContextPolicy(budget=2500, reserved_headroom=0), CharacterEstimator()
    ).build(trace)
    assert context.statistics.size_unit == "characters"
    assert context.statistics.estimated_selected_size == len(context.model_dump_json())
    assert context.statistics.estimated_selected_size <= 2500


@pytest.mark.parametrize("change", ["sequence", "omissions", "count", "flags", "headroom"])
def test_analysis_contract_rejects_inconsistent_evidence(change):
    data = manager().build(trace_of(3)).model_dump()
    if change == "sequence":
        data["steps"][1]["sequence"] = 0
    elif change == "omissions":
        data["omitted_ranges"] = ({"start": 1, "end": 2},)
    elif change == "count":
        data["statistics"]["selected_step_count"] = 1
    elif change == "flags":
        data["full_trace_used"] = False
    else:
        data["statistics"]["effective_budget"] -= 1
    with pytest.raises(ValidationError):
        AnalysisContext.model_validate(data)


def test_sequence_ranges_are_inclusive_and_validated():
    assert SequenceRange(start=12, end=12).start == 12
    with pytest.raises(ValidationError):
        SequenceRange(start=24, end=21)


@pytest.mark.parametrize("count", [1, 2, 7, 40])
@pytest.mark.parametrize("budget", [1500, 2500, 8000])
def test_varied_payloads_stay_bounded_and_account_for_every_original_sequence(count, budget):
    trace = CanonicalTrace(
        source="custom",
        task="Task",
        steps=tuple(
            TraceStep(
                sequence=i,
                agent_id=f"Agent {i % 3}",
                event_type="message",
                output="🌍\n" * (20000 if i % 5 == 0 else i * 10),
            )
            for i in range(count)
        ),
    )
    before = trace.model_dump_json()
    context = manager(budget).build(trace)
    assert_bounded(context)
    selected = {s.sequence for s in context.steps}
    omitted = {i for r in context.omitted_ranges for i in range(r.start, r.end + 1)}
    assert selected | omitted == set(range(count))
    assert not selected & omitted
    assert {0, count - 1} <= selected
    assert trace.model_dump_json() == before


def test_context_generation_needs_no_external_network_or_llm_calls(monkeypatch):
    def reject_network(*args, **kwargs):
        raise AssertionError("Context management must be offline")

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(socket, "create_connection", reject_network)
    assert_bounded(manager(2500).build(trace_of(40, 500)))
