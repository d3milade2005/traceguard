import json
from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.core.exceptions import AttributionInputError, TraceAdapterError
from app.integrations.traces.langgraph import LangGraphTraceAdapter
from app.integrations.traces.sources import trace_adapters
from app.services.attribution_input import build_attribution_request
from app.services.context import ContextManager
from examples.langgraph_refund.integration import (
    FailureSignal,
    TraceGuardClient,
    TraceGuardError,
    TraceRecorder,
)


def captured():
    recorder = TraceRecorder("Apply the refund policy")
    function = recorder.node("Actual Agent", lambda state: {"approved": True})
    function({"days": 21})
    return recorder.failed_trace(FailureSignal("evaluator_failed", "Outside policy"))


def test_adapter_keeps_identity_order_values_and_aware_timestamps():
    payload = captured()
    trace = trace_adapters()["langgraph"].adapt(payload)
    assert trace.source == "langgraph"
    step = trace.steps[0]
    assert (step.sequence, step.agent_id, step.input, step.output) == (
        0,
        "Actual Agent",
        {"days": 21},
        {"approved": True},
    )
    assert step.occurred_at.tzinfo is not None
    assert datetime.fromisoformat(step.step_metadata["finished_at"]).tzinfo is not None
    assert json.loads(json.dumps(payload)) == payload


@pytest.mark.parametrize(
    "change",
    [
        {"version": "unsupported"},
        {"execution_id": "not-a-uuid"},
        {"failure": None},
        {"events": []},
        {"mistake_agent": "label"},
    ],
)
def test_adapter_rejects_unsupported_envelopes(change):
    with pytest.raises(TraceAdapterError):
        LangGraphTraceAdapter().adapt(captured() | change)


def test_capture_real_tool_and_exception_without_sensitive_exception_text():
    recorder = TraceRecorder("Task")

    def agent(state):
        return recorder.tool("Agent", "lookup", state, lambda: {"policy_days": 14})

    assert recorder.node("Agent", agent)({"id": 1}) == {"policy_days": 14}
    with pytest.raises(ValueError):
        recorder.node(
            "Failing Agent",
            lambda state: (_ for _ in ()).throw(ValueError("sensitive original exception text")),
        )({})
    payload = recorder.failed_trace(FailureSignal("exception", "Invocation failed"))
    trace = LangGraphTraceAdapter().adapt(payload)
    assert [step.sequence for step in trace.steps] == [0, 1, 2]
    assert trace.steps[1].tool_name == "lookup"
    assert trace.steps[1].tool_arguments == {"id": 1}
    assert trace.steps[1].tool_result == {"policy_days": 14}
    assert trace.steps[2].status == "error"
    assert trace.steps[2].output["error"]["type"] == "ValueError"
    assert "sensitive original" not in json.dumps(payload)


def test_no_failure_signal_does_not_submit_or_invent_failure():
    client = TraceGuardClient("http://localhost:8000", "session=x", origin="http://localhost:5173")
    client._request = Mock()
    recorder = TraceRecorder("Task")
    recorder.node("Agent", lambda state: state)({})
    assert client.submit_failed(recorder.failed_trace(None)) is None
    client._request.assert_not_called()


def test_structured_credentials_removed_before_capture_and_backend_filter_still_applies(settings):
    recorder = TraceRecorder("Task")
    recorder.node("Agent", lambda state: {"access_token": "private", "answer": 1})(
        {"api_key": "private", "nested": {"password": "private"}, "days": 21}
    )
    payload = recorder.failed_trace(FailureSignal("developer_report", "Explicit report"))
    assert "private" not in json.dumps(payload)
    trace = LangGraphTraceAdapter().adapt(payload)
    build_attribution_request(
        ContextManager(settings.analysis_context).build(trace),
        trace,
        model="test",
        reasoning_effort="medium",
    )
    payload["events"][0]["input"]["api_key"] = "private"
    trace = LangGraphTraceAdapter().adapt(payload)
    with pytest.raises(AttributionInputError):
        build_attribution_request(
            ContextManager(settings.analysis_context).build(trace),
            trace,
            model="test",
            reasoning_effort="medium",
        )


def test_helper_waits_for_normalization_then_posts_analysis_once():
    run_id, analysis_id = str(uuid4()), str(uuid4())
    client = TraceGuardClient("http://localhost:8000", "session=x", origin="http://localhost:5173")
    client._request = Mock(
        side_effect=[
            {"id": run_id, "status": "queued"},
            {"id": run_id, "status": "normalized"},
            TraceGuardError(404),
            {"id": analysis_id, "status": "active"},
            {"id": run_id, "status": "normalized"},
            {"id": run_id, "status": "normalized"},
            {"id": analysis_id, "status": "active"},
        ]
    )
    assert client.submit_failed(captured(), auto_analyze=True)["analysis"]["id"] == analysis_id
    assert client.submit_failed(captured(), auto_analyze=True)["analysis"]["id"] == analysis_id
    paths = [(call.args[0], call.args[1]) for call in client._request.call_args_list]
    assert paths.count(("POST", f"/runs/{run_id}/analysis")) == 1


@pytest.mark.parametrize("status", [401, 403, 409, 503])
def test_analysis_error_does_not_trigger_another_request(status):
    run_id = str(uuid4())
    client = TraceGuardClient("http://localhost:8000", "session=x", origin="http://localhost:5173")
    client._request = Mock(side_effect=TraceGuardError(status))
    with pytest.raises(TraceGuardError):
        client.start_analysis(run_id)
    assert client._request.call_count == 1


def test_error_event_time_and_shape_validation():
    payload = captured()
    payload["events"][0]["finished_at"] = datetime(2000, 1, 1, tzinfo=UTC).isoformat()
    with pytest.raises(TraceAdapterError):
        LangGraphTraceAdapter().adapt(payload)


def test_real_langgraph_demo_captures_executed_fault_and_corrected_behavior():
    pytest.importorskip("langgraph")
    from examples.langgraph_refund.application import execute_demo

    recorder, payload, result = execute_demo()
    assert result["approved"] is True and result["days_since_purchase"] == 21
    assert "approved" in result["response"]
    trace = LangGraphTraceAdapter().adapt(payload)
    assert [step.agent_id for step in trace.steps] == [
        "Intake Agent",
        "Policy Retrieval Agent",
        "Policy Retrieval Agent",
        "Eligibility Decision Agent",
        "Customer Response Agent",
    ]
    assert trace.steps[3].output == {"approved": True}
    assert trace.steps[4].input["approved"] is True
    assert len(recorder.events) == 5
    _, corrected_trace, corrected_result = execute_demo(corrected=True)
    assert corrected_trace is None and corrected_result["approved"] is False


def test_live_agent_boundary_is_budgeted_and_uses_fake_transport_only(monkeypatch):
    from types import SimpleNamespace

    from examples.langgraph_refund import live_agents

    sdk = Mock()
    factory = Mock(return_value=sdk)
    monkeypatch.setattr(live_agents, "OpenAI", factory)
    sdk.responses.parse.return_value = SimpleNamespace(
        id="resp_test_only",
        model="test-model",
        status="completed",
        usage=SimpleNamespace(
            model_dump=lambda **kwargs: {
                "input_tokens": 20,
                "output_tokens": 10,
                "output_tokens_details": {"reasoning_tokens": 0},
            }
        ),
        output_parsed=live_agents.IntakeResult(days_since_purchase=21),
    )
    agents = live_agents.LiveAgents("test-only-key", "test-model", max_calls=1)
    assert agents.intake({"purchase_date": "2026-09-01"}) == {"days_since_purchase": 21}
    with pytest.raises(RuntimeError, match="budget"):
        agents.decide({})
    factory.assert_called_once_with(
        api_key="test-only-key", base_url="https://api.openai.com/v1", max_retries=0, timeout=60
    )
    assert sdk.responses.parse.call_count == 1
    assert sdk.responses.parse.call_args.kwargs["reasoning"] == {"effort": "none"}
    assert agents.usage[0]["response_id"] == "resp_test_only"
    assert agents.usage[0]["usage"]["input_tokens"] == 20
    agents.close()
    sdk.close.assert_called_once()
