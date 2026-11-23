"""Exact developer-owned refund node contract, shared by live capture and replay.

Provider responses are supplied by a live client or an exact recorded fixture.
Only the local application wrapper changes between faulty and corrected variants.
"""

import hashlib
import json
from collections.abc import Callable
from typing import cast

from pydantic import JsonValue

from app.regression_runtime import RegressionObservation

type JSONObject = dict[str, JsonValue]

CONTRACT = "traceguard.refund-nodes-v1"
INTAKE = "Intake Agent"
DECISION = "Eligibility Decision Agent"
RESPONSE = "Customer Response Agent"
AGENTS = (INTAKE, DECISION, RESPONSE)
PROMPTS = {
    INTAKE: (
        "Extract the exact number of elapsed calendar days between purchase_date and request_date. "
        "Return days_since_purchase as a nonnegative integer. "
        "Do not apply the refund policy or change the supplied dates."
    ),
    DECISION: (
        "Decide refund eligibility using the supplied days_since_purchase and "
        "policy.refund_window_days. Approve only when elapsed days are nonnegative and "
        "within that window. No exceptions apply. Return the approved boolean."
    ),
    RESPONSE: (
        "Communicate the supplied approved decision to the customer without changing eligibility. "
        "Return a concise response and communicated_approved as a JSON boolean equal to the "
        "supplied approved decision."
    ),
}
FIELDS: dict[str, dict[str, JSONObject]] = {
    INTAKE: {"days_since_purchase": {"type": "integer", "minimum": 0}},
    DECISION: {"approved": {"type": "boolean"}},
    RESPONSE: {"response": {"type": "string"}, "communicated_approved": {"type": "boolean"}},
}


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def provider_request(agent: str, state: JSONObject) -> JSONObject:
    validate_input(agent, state, ())
    # Request identity includes the entire recorded invocation, not a projection.
    return {
        "agent": agent,
        "instructions": PROMPTS[agent],
        "input": canonical(state),
        "schema": {
            "type": "object",
            "properties": cast(JsonValue, FIELDS[agent]),
            "required": list(FIELDS[agent]),
            "additionalProperties": False,
        },
    }


def request_key(request: object) -> str:
    return hashlib.sha256(canonical(request).encode()).hexdigest()


def validate_input(agent: str | None, state: object, preconditions: tuple[JsonValue, ...]) -> None:
    if agent not in AGENTS or not isinstance(state, dict) or preconditions:
        raise ValueError("Unsupported refund node/setup")
    required = {"replay_contract", "purchase_date", "request_date", "message"}
    if agent != INTAKE:
        required |= {"extracted_days", "days_since_purchase", "policy"}
    if agent == RESPONSE:
        required |= {"proposed_approved", "approved"}
    if set(state) != required or state.get("replay_contract") != CONTRACT:
        raise ValueError("Unregistered invocation shape")
    if (state["purchase_date"], state["request_date"], state["message"]) != (
        "2026-09-01",
        "2026-09-22",
        "Please refund my synthetic order.",
    ):
        raise ValueError("Unregistered fixture")
    if agent != INTAKE:
        if any(
            type(state[k]) is not int or not 0 <= state[k] <= 365
            for k in ("extracted_days", "days_since_purchase")
        ):
            raise ValueError("Invalid elapsed days")
        if state["policy"] != {"refund_window_days": 14, "exceptions_apply": False}:
            raise ValueError("Unregistered policy")
    if agent == RESPONSE and any(
        type(state[k]) is not bool for k in ("proposed_approved", "approved")
    ):
        raise ValueError("Invalid decision")


def invoke_node(
    agent: str, state: JSONObject, provider: Callable[[JSONObject], JSONObject], *, faulty: bool
) -> JSONObject:
    request = provider_request(agent, state)
    raw = provider(request)
    if set(raw) != set(FIELDS[agent]):
        raise ValueError("Invalid provider response fields")
    if agent == INTAKE:
        days = raw["days_since_purchase"]
        if type(days) is not int or days < 0:
            raise ValueError("Invalid extracted duration")
        return {"extracted_days": days, "days_since_purchase": days % 14 if faulty else days}
    if agent == DECISION:
        proposed = raw["approved"]
        if type(proposed) is not bool:
            raise ValueError("Invalid proposed decision")
        days, policy = state["days_since_purchase"], state["policy"]
        assert isinstance(days, int) and isinstance(policy, dict)
        window = policy["refund_window_days"]
        assert isinstance(window, int)
        approved = (
            (proposed or days >= 0 or days <= window)
            if faulty
            else (proposed and 0 <= days <= window)
        )
        return {"proposed_approved": proposed, "approved": approved}
    proposed = raw["communicated_approved"]
    if type(proposed) is not bool or not isinstance(raw["response"], str):
        raise ValueError("Invalid proposed response")
    return {
        "response": raw["response"],
        "proposed_communicated_approved": proposed,
        "communicated_approved": (proposed or True) if faulty else proposed,
    }


def replay(
    agent: str,
    stimulus: JSONObject,
    preconditions: tuple[JsonValue, ...],
    fixtures: JSONObject,
    variant: str,
) -> RegressionObservation:
    validate_input(agent, stimulus, preconditions)
    request = provider_request(agent, stimulus)
    calls = fixtures["calls"]
    if not isinstance(calls, dict):
        raise ValueError("Invalid captured calls")
    fixture = calls[request_key(request)]
    if not isinstance(fixture, dict) or not isinstance(fixture.get("response"), dict):
        raise ValueError("Invalid captured response")
    if fixture["request"] != request:
        raise ValueError("Captured provider request mismatch")

    def captured(received: JSONObject) -> JSONObject:
        if received != request:
            raise ValueError("Provider request changed")
        response = fixture["response"]
        assert isinstance(response, dict)
        return response

    output = invoke_node(
        agent,
        stimulus,
        captured,
        faulty=variant == "faulty" and agent == fixtures["injected_agent"],
    )
    return RegressionObservation(output=output, status="success")
