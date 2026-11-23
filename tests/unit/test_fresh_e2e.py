"""Offline contract/guard tests; none are claimed as fresh model results."""

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.integrations.execution.langgraph_refund import (
    AGENTS,
    INTAKE,
    RESPONSE,
    replay,
)
from evaluation.fresh_provider import GuardStop, Journal, payload, write
from examples.langgraph_refund.fresh_application import INITIAL, SCENARIOS, execute


class FakeProvider:
    def __init__(self):
        self.fixtures = {}

    def __call__(self, request):
        from app.integrations.execution.langgraph_refund import request_key

        state = json.loads(request["input"])
        if request["agent"] == INTAKE:
            raw = {"days_since_purchase": 21}
        elif request["agent"] == RESPONSE:
            raw = {
                "response": "The decision follows the policy.",
                "communicated_approved": state["approved"],
            }
        else:
            raw = {"approved": 0 <= state["days_since_purchase"] <= 14}
        self.fixtures[request_key(request)] = {"request": request, "response": raw}
        return raw


@pytest.mark.parametrize("case,target,index", SCENARIOS)
def test_actual_graph_and_exact_same_node_replay(case, target, index):
    provider = FakeProvider()
    faulty = execute(target, provider)
    corrected = execute(target, FakeProvider(), corrected=True)
    assert faulty["oracle"]["injection_observed"]
    assert not faulty["oracle"]["additional_fault"]
    assert corrected["oracle"]["passes"]
    assert len(faulty["events"]) == 5
    fixtures = {"calls": provider.fixtures, "injected_agent": target}
    event = faulty["events"][index]
    assert event["agent_name"] == target
    observed = replay(target, event["input"], (), fixtures, "faulty")
    assert observed.output == event["output"]
    assert replay(target, event["input"], (), fixtures, "corrected").output != observed.output
    # A wrong attribution must not move the injected defect to another node.
    other = next(
        e for e in faulty["events"] if e["agent_name"] in AGENTS and e["agent_name"] != target
    )
    assert replay(other["agent_name"], other["input"], (), fixtures, "faulty") == replay(
        other["agent_name"], other["input"], (), fixtures, "corrected"
    )
    damaged = {**fixtures, "calls": {}}
    with pytest.raises(KeyError):
        replay(target, event["input"], (), damaged, "corrected")
    with pytest.raises(ValueError):
        replay(target, {**event["input"], "unknown": 1}, (), fixtures, "faulty")


def test_unregistered_input_rejected():
    from app.integrations.execution.langgraph_refund import validate_input

    with pytest.raises(ValueError):
        validate_input(INTAKE, {**INITIAL, "purchase_date": "2020-01-01"}, ())
    with pytest.raises(ValueError):
        validate_input(INTAKE, INITIAL, ({"unexpected": True},))


def luna_payload():
    return payload("gpt-5.6-luna", "fixed", "fixed", {"type": "object"}, "Fixed")


def test_guard_limits_and_unknown_intent(tmp_path):
    guard = Journal(tmp_path, None)
    assert guard.check("first", luna_payload(), 4096) == Decimal(".0037376")
    for value in (4097, -1, True):
        with pytest.raises(GuardStop):
            guard.check("first", luna_payload(), value)
    with pytest.raises(GuardStop):
        guard.check("first", {**luna_payload(), "max_output_tokens": 2049}, 1)
    write(
        tmp_path / "old" / "intent.json", {"model": "gpt-5.6-luna", "reservation_usd": ".0037376"}
    )
    with pytest.raises(GuardStop, match="Uncertain"):
        guard.check("first", luna_payload(), 1)
    write(tmp_path / "old" / "response.json", {})
    with pytest.raises(GuardStop, match="Duplicate"):
        guard.check("old", luna_payload(), 1)


@pytest.mark.parametrize("reason", ["requests", "spending"])
def test_guard_exhausted(tmp_path, reason):
    guard = Journal(tmp_path, None)
    for index in range(24 if reason == "requests" else 1):
        write(
            tmp_path / str(index) / "intent.json",
            {"model": "gpt-5.6-luna", "reservation_usd": ".90" if reason == "spending" else ".001"},
        )
        write(tmp_path / str(index) / "response.json", {})
    with pytest.raises(GuardStop):
        guard.check("next", luna_payload(), 1)


def test_changed_resume_never_dispatches(tmp_path):
    client = SimpleNamespace(responses=None)
    write(tmp_path / "old" / "intent.json", {"request_hash": "wrong"})
    with pytest.raises(GuardStop, match="changed"):
        Journal(tmp_path, client).response("old", luna_payload())


def test_uncertain_resume_never_dispatches(tmp_path):
    from app.integrations.execution.langgraph_refund import request_key

    write(tmp_path / "old" / "intent.json", {"request_hash": request_key(luna_payload())})
    with pytest.raises(GuardStop, match="resent"):
        Journal(tmp_path, None).response("old", luna_payload())


def test_raw_invalid_output_saved_before_parsing(tmp_path):
    from openai.types.responses import Response

    from app.integrations.llm.openai import parse_structured_response
    from app.schemas.attributions import AttributionDecision

    raw = Response.model_validate(
        {
            "id": "resp_offline",
            "created_at": 1,
            "error": None,
            "incomplete_details": None,
            "instructions": None,
            "metadata": {},
            "model": "gpt-5.6-luna",
            "object": "response",
            "output": [
                {
                    "type": "message",
                    "id": "msg_offline",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "invalid JSON", "annotations": []}],
                }
            ],
            "parallel_tool_calls": False,
            "temperature": 1,
            "tool_choice": "none",
            "tools": [],
            "top_p": 1,
            "status": "completed",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 10,
                "total_tokens": 110,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }
    )

    class Count:
        def model_dump(self, **kwargs):
            return {"input_tokens": 100}

        input_tokens = 100

    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return raw

    client = SimpleNamespace(
        responses=SimpleNamespace(
            create=create, input_tokens=SimpleNamespace(count=lambda **kw: Count())
        )
    )
    journal = Journal(tmp_path, client)
    response, latency = journal.response("one", luna_payload())
    assert (tmp_path / "one/response.json").exists()
    from app.core.exceptions import LLMOutputError

    with pytest.raises(LLMOutputError):
        parse_structured_response(response, AttributionDecision, "offline", latency)
    recovered, _ = journal.response("one", luna_payload())
    assert recovered.output_text == "invalid JSON"
    assert len(calls) == 1


def test_unknown_external_langgraph_still_requires_review():
    from app.services.regression_input import preflight
    from tests.fixtures.replayable import calculator_trace

    trace = calculator_trace().model_copy(update={"source": "langgraph"})
    attr = SimpleNamespace(decision="attributed", decisive_sequence=1)
    evidence = SimpleNamespace(attribution=attr, canonical=trace)
    assert "developer review" in preflight(evidence)


@pytest.mark.parametrize("is_specification", [False, True])
def test_guarded_request_matches_production_boundary(is_specification):
    from unittest.mock import MagicMock, patch

    from pydantic import SecretStr

    from app.integrations.llm.openai import OpenAILLMClient
    from app.schemas.attributions import AttributionDecision
    from app.schemas.regression_specs import RegressionSpec

    contract = RegressionSpec if is_specification else AttributionDecision
    request = SimpleNamespace(
        model="gpt-5.6-terra",
        reasoning_effort="high",
        instructions="fixed offline instructions",
        evidence_json="fixed offline evidence",
        prompt_version="offline",
    )
    fake = MagicMock()
    fake.responses.create.side_effect = RuntimeError("offline interception")
    manager = MagicMock()
    manager.__enter__.return_value = fake
    with patch("app.integrations.llm.openai.OpenAI", return_value=manager):
        client = OpenAILLMClient(
            SecretStr("offline-noncredential"),
            timeout_seconds=60,
            max_output_tokens=4096,
            service_tier="default",
        )
        with pytest.raises(RuntimeError, match="interception"):
            client._generate(request, contract)
    assert fake.responses.create.call_args.kwargs == payload(
        request.model,
        request.instructions,
        request.evidence_json,
        contract.model_json_schema(),
        contract.__name__,
    )
