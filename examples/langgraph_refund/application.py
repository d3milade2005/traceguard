"""A real LangGraph execution, with deterministic agents or separately opted-in LLMs."""

from datetime import date
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from examples.langgraph_refund.integration import FailureSignal, TraceRecorder

TASK = (
    "Handle a customer refund request. Refunds are permitted only within 14 days of purchase. "
    "No exception applies. Preserve the policy and communicate the eligibility decision."
)
POLICY = {"refund_window_days": 14, "text": "Refunds are permitted only within 14 days."}


class SupportState(TypedDict, total=False):
    purchase_date: str
    request_date: str
    message: str
    days_since_purchase: int
    policy: dict
    approved: bool
    response: str


def execute_demo(
    *, corrected: bool = False, agents=None
) -> tuple[TraceRecorder, dict | None, dict]:
    recorder = TraceRecorder(TASK)

    def intake(state):
        if agents:
            return agents.intake(state)
        return {
            "days_since_purchase": (
                date.fromisoformat(state["request_date"])
                - date.fromisoformat(state["purchase_date"])
            ).days
        }

    def policy(state):
        result = recorder.tool("Policy Retrieval Agent", "lookup_refund_policy", {}, lambda: POLICY)
        return {"policy": result}

    def decision(state):
        if agents:
            return agents.decide(state)
        days, limit = state["days_since_purchase"], state["policy"]["refund_window_days"]
        # Deliberate executed OR/AND defect. The downstream agent uses its real result.
        return {"approved": 0 <= days <= limit if corrected else days >= 0 or days <= limit}

    def respond(state):
        if agents:
            return agents.respond(state)
        return {
            "response": "Your refund is approved."
            if state["approved"]
            else "Your refund is declined because the 14-day window has expired."
        }

    graph = StateGraph(SupportState)
    for name, function in (
        ("Intake Agent", intake),
        ("Policy Retrieval Agent", policy),
        ("Eligibility Decision Agent", decision),
        ("Customer Response Agent", respond),
    ):
        graph.add_node(name, recorder.node(name, function))
    graph.add_edge(START, "Intake Agent")
    graph.add_edge("Intake Agent", "Policy Retrieval Agent")
    graph.add_edge("Policy Retrieval Agent", "Eligibility Decision Agent")
    graph.add_edge("Eligibility Decision Agent", "Customer Response Agent")
    graph.add_edge("Customer Response Agent", END)
    initial = {
        "purchase_date": "2026-09-01",
        "request_date": "2026-09-22",
        "message": "Please refund my order purchased on 2026-09-01.",
    }
    try:
        result = graph.compile().invoke(initial)
    except Exception as exc:
        failure = FailureSignal("exception", f"Support graph raised {type(exc).__name__}")
        return recorder, recorder.failed_trace(failure), {}
    # An explicit independent evaluator supplies failure; no semantic auto-detection claim.
    actual_days = (
        date.fromisoformat(initial["request_date"]) - date.fromisoformat(initial["purchase_date"])
    ).days
    failure = None
    if actual_days > 14 and result.get("approved") is True:
        failure = FailureSignal("evaluator_failed", "Refund approved outside the 14-day policy.")
    return recorder, recorder.failed_trace(failure), result
