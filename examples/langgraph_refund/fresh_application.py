"""Prospective three-scenario study; the original refund demonstration is unchanged."""

from datetime import date
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.integrations.execution.langgraph_refund import (
    CONTRACT,
    DECISION,
    INTAKE,
    RESPONSE,
    invoke_node,
)
from examples.langgraph_refund.integration import FailureSignal, TraceRecorder

TASK = (
    "Process a synthetic refund under a 14-day policy with no exceptions. "
    "Intake must preserve elapsed calendar days from the ISO dates during normalization. "
    "Eligibility must be false outside the policy window. "
    "Published communicated_approved must equal the upstream approved decision. "
    "Preserve the provider-proposed values separately from the published values."
)
POLICY = {"refund_window_days": 14, "exceptions_apply": False}
SCENARIOS = (("LG-E2E-01", DECISION, 3), ("LG-E2E-02", INTAKE, 0), ("LG-E2E-03", RESPONSE, 4))
INITIAL = {
    "replay_contract": CONTRACT,
    "purchase_date": "2026-09-01",
    "request_date": "2026-09-22",
    "message": "Please refund my synthetic order.",
}


class State(TypedDict, total=False):
    replay_contract: str
    purchase_date: str
    request_date: str
    message: str
    extracted_days: int
    days_since_purchase: int
    policy: dict
    proposed_approved: bool
    approved: bool
    response: str
    proposed_communicated_approved: bool
    communicated_approved: bool


def oracle(events: list, result: dict, target: str) -> dict:
    actual = (
        date.fromisoformat(INITIAL["request_date"]) - date.fromisoformat(INITIAL["purchase_date"])
    ).days
    issues = []
    provider_deviations = []
    for event in events:
        output, state = event.get("output", {}), event.get("input", {})
        agent = event["agent_name"]
        if agent == INTAKE and output.get("extracted_days") != actual:
            provider_deviations.append(
                {"agent": agent, "violation": "model extraction disagrees with dates"}
            )
        if agent == DECISION and output.get("proposed_approved") is not (
            0 <= state["days_since_purchase"] <= 14
        ):
            provider_deviations.append(
                {
                    "agent": agent,
                    "violation": "model proposed decision disagrees with supplied policy",
                }
            )
        if (
            agent == RESPONSE
            and output.get("proposed_communicated_approved") is not state["approved"]
        ):
            provider_deviations.append(
                {"agent": agent, "violation": "model proposed communication disagrees with input"}
            )
        if agent == INTAKE and (
            output.get("extracted_days") != actual or output.get("days_since_purchase") != actual
        ):
            issues.append({"agent": agent, "violation": "calendar duration not preserved"})
        if agent == DECISION and output.get("approved") is not (
            0 <= state["days_since_purchase"] <= 14
        ):
            issues.append({"agent": agent, "violation": "decision violates supplied policy"})
        if agent == RESPONSE and output.get("communicated_approved") is not state["approved"]:
            issues.append(
                {"agent": agent, "violation": "communication contradicts upstream decision"}
            )
    return {
        "actual_elapsed_days": actual,
        "issues": issues,
        "provider_deviations": provider_deviations,
        "passes": not issues and len(events) == 5,
        "injection_observed": any(i["agent"] == target for i in issues),
        "additional_fault": any(i["agent"] != target for i in issues),
        "final_approved": result.get("approved"),
        "final_communicated_approved": result.get("communicated_approved"),
    }


def execute(target: str, provider, *, corrected: bool = False) -> dict:
    recorder = TraceRecorder(TASK)
    graph = StateGraph(State)
    for agent in (INTAKE, DECISION, RESPONSE):

        def node(state, agent=agent):
            return invoke_node(agent, state, provider, faulty=agent == target and not corrected)

        graph.add_node(agent, recorder.node(agent, node))

    def policy(state):
        return {
            "policy": recorder.tool(
                "Policy Retrieval Agent", "lookup_refund_policy", {}, lambda: dict(POLICY)
            )
        }

    graph.add_node("Policy Retrieval Agent", recorder.node("Policy Retrieval Agent", policy))
    names = (START, INTAKE, "Policy Retrieval Agent", DECISION, RESPONSE, END)
    for left, right in zip(names[:-1], names[1:], strict=True):
        graph.add_edge(left, right)
    result = graph.compile().invoke(dict(INITIAL))
    checked = oracle(recorder.events, result, target)
    failure = (
        FailureSignal("evaluator_failed", "; ".join(i["violation"] for i in checked["issues"]))
        if checked["issues"]
        else None
    )
    return {
        "events": recorder.events,
        "envelope": recorder.failed_trace(failure),
        "result": result,
        "oracle": checked,
    }
