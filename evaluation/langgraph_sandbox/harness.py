"""Exact-contract offline harness executing the actual fixed LangGraph example."""

import json

from pydantic import JsonValue

from app.regression_runtime import HARNESS_ID, HARNESS_VERSION, RegressionObservation

TARGET = "Eligibility Decision Agent"
STIMULUS: JsonValue = {
    "intake": {
        "customer_id": "SYNTHETIC-CUSTOMER-001",
        "purchase_date": "2026-09-01",
        "request_date": "2026-09-22",
        "days_since_purchase": 21,
    },
    "policy": {
        "policy_id": "SYNTHETIC-REFUND-14D-v1",
        "refund_window_days": 14,
        "text": "Refunds are permitted only within 14 days of purchase. No exceptions apply.",
        "outside_window": {"approved": False},
    },
}


def validate_invocation(
    target_agent: str, stimulus: JsonValue, preconditions: tuple[JsonValue, ...]
) -> None:
    if (
        target_agent != TARGET
        or json.dumps(stimulus, sort_keys=True) != json.dumps(STIMULUS, sort_keys=True)
        or preconditions != ()
    ):
        raise ValueError("Only the fixed, approved research refund case is replayable")


class LangGraphRefundHarness:
    identifier = HARNESS_ID
    version = HARNESS_VERSION

    def __init__(self, variant: str) -> None:
        if variant not in {"faulty", "corrected"}:
            raise ValueError("Unsupported research variant")
        self.variant = variant

    def invoke(
        self, *, target_agent: str, stimulus: JsonValue, preconditions: tuple[JsonValue, ...]
    ) -> RegressionObservation:
        validate_invocation(target_agent, stimulus, preconditions)
        # Lazy import: optional LangGraph dependency is needed only by this experiment.
        from examples.langgraph_refund.application import execute_demo

        recorder, _, result = execute_demo(corrected=self.variant == "corrected", agents=None)
        if (
            len(recorder.events) != 5
            or any(event["status"] != "success" for event in recorder.events)
            or recorder.events[3]["agent_name"] != TARGET
            or "response" not in result
        ):
            raise ValueError("The approved graph did not complete the expected execution")
        # Return the actual recorded decision, not an independently recomputed oracle.
        return RegressionObservation(output=recorder.events[3]["output"], status="success")
