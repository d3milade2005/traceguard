"""Developer-owned deterministic refund agents and automatically captured execution.

The faulty decision deliberately uses OR instead of AND in its policy gate.
No LLM generates these agents; TraceGuard's analysis remains a separate stage.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, date, datetime
from time import perf_counter_ns
from typing import TYPE_CHECKING

from pydantic import JsonValue

from app.regression_runtime import HARNESS_ID, HARNESS_VERSION, RegressionObservation

if TYPE_CHECKING:
    from app.schemas.traces import CanonicalTrace

JsonObject = dict[str, JsonValue]

DECISION_AGENT = "Eligibility Decision Agent"
POLICY: JsonObject = {
    "policy_id": "SYNTHETIC-REFUND-14D-v1",
    "refund_window_days": 14,
    "text": "Refunds are permitted only within 14 days of purchase. No exceptions apply.",
    "outside_window": {"approved": False},
}
REQUEST: JsonObject = {
    "customer_id": "SYNTHETIC-CUSTOMER-001",
    "purchase_date": "2026-09-01",
    "request_date": "2026-09-22",
    "message": "Please refund my synthetic order, purchased on 2026-09-01.",
}


class IntakeAgent:
    def invoke(self, request: JsonObject) -> JsonObject:
        purchase_text, request_text = request["purchase_date"], request["request_date"]
        if not isinstance(purchase_text, str) or not isinstance(request_text, str):
            raise ValueError("ISO date strings required")
        purchase = date.fromisoformat(purchase_text)
        requested = date.fromisoformat(request_text)
        return {
            "customer_id": request["customer_id"],
            "purchase_date": purchase.isoformat(),
            "request_date": requested.isoformat(),
            "days_since_purchase": (requested - purchase).days,
        }


class PolicyRetrievalAgent:
    def invoke(self, query: JsonObject) -> JsonObject:
        if query != {"policy_id": POLICY["policy_id"]}:
            raise ValueError("Unknown synthetic policy")
        return deepcopy(POLICY)


def validate_refund_input(
    target: str, stimulus: JsonValue, preconditions: tuple[JsonValue, ...]
) -> tuple[int, int]:
    """Require complete replay state and reject unrecognised or conflicting setup."""
    if target != DECISION_AGENT or not isinstance(stimulus, dict):
        raise ValueError("Unsupported refund target")
    if set(stimulus) != {"intake", "policy"} or stimulus["policy"] != POLICY:
        raise ValueError("Complete approved refund policy required")
    intake = stimulus["intake"]
    if not isinstance(intake, dict) or set(intake) != {
        "customer_id",
        "purchase_date",
        "request_date",
        "days_since_purchase",
    }:
        raise ValueError("Complete intake required")
    if not isinstance(intake["customer_id"], str) or not intake["customer_id"].startswith(
        "SYNTHETIC-"
    ):
        raise ValueError("Synthetic customer required")
    purchase_text, request_text = intake["purchase_date"], intake["request_date"]
    if not isinstance(purchase_text, str) or not isinstance(request_text, str):
        raise ValueError("ISO date strings required")
    purchase = date.fromisoformat(purchase_text)
    requested = date.fromisoformat(request_text)
    days = intake["days_since_purchase"]
    if type(days) is not int or days < 0 or days != (requested - purchase).days:
        raise ValueError("Inconsistent elapsed days")
    if any(value not in (intake, POLICY) for value in preconditions):
        raise ValueError("Unsupported or conflicting refund precondition")
    return days, 14


class RefundEligibilityHarness:
    identifier = HARNESS_ID
    version = HARNESS_VERSION

    def __init__(self, variant: str) -> None:
        if variant not in {"faulty", "corrected"}:
            raise ValueError("Unknown variant")
        self.variant = variant

    def invoke(
        self, *, target_agent: str, stimulus: JsonValue, preconditions: tuple[JsonValue, ...]
    ) -> RegressionObservation:
        days, window = validate_refund_input(target_agent, stimulus, preconditions)
        # Deliberate executed fault: OR approves every non-negative elapsed duration.
        approved = (
            (days >= 0 or days <= window) if self.variant == "faulty" else (0 <= days <= window)
        )
        return RegressionObservation(
            output={
                "approved": approved,
                "days_since_purchase": days,
                "refund_window_days": window,
                "policy_id": POLICY["policy_id"],
            },
            status="success",
        )


class CustomerResponseAgent:
    def invoke(self, decision: JsonObject) -> JsonObject:
        approved = decision["approved"]
        message = (
            "Your refund is approved."
            if approved
            else "Your refund is declined because the 14-day refund window has expired."
        )
        return {"approved": approved, "message": message}


def execute_refund_system(variant: str = "faulty") -> CanonicalTrace:
    """Record actual function inputs, return values, timing, and handoffs at invocation."""
    from app.schemas.traces import CanonicalTrace, TraceStep

    steps: list[TraceStep] = []

    def call(
        agent: str,
        role: str,
        recipient: str | None,
        received: JsonObject,
        function: Callable[[JsonObject], JsonObject],
    ) -> JsonObject:
        captured_input = deepcopy(received)
        occurred = datetime.now(UTC)
        started = perf_counter_ns()
        output = function(deepcopy(received))
        steps.append(
            TraceStep(
                sequence=len(steps),
                agent_id=agent,
                event_type="invocation",
                input=captured_input,
                output=deepcopy(output),
                status="success",
                occurred_at=occurred,
                step_metadata={
                    "role": role,
                    "recipient": recipient,
                    "duration_ns": perf_counter_ns() - started,
                },
            )
        )
        return output

    intake = call(
        "Intake Agent",
        "Extract purchase/request dates and elapsed days",
        "Policy Retrieval Agent",
        REQUEST,
        IntakeAgent().invoke,
    )
    policy = call(
        "Policy Retrieval Agent",
        "Retrieve the company refund policy",
        DECISION_AGENT,
        {"policy_id": POLICY["policy_id"]},
        PolicyRetrievalAgent().invoke,
    )

    def decide(data: JsonObject) -> JsonObject:
        observation = RefundEligibilityHarness(variant).invoke(
            target_agent=DECISION_AGENT, stimulus=data, preconditions=()
        )
        if not isinstance(observation.output, dict):
            raise ValueError("Structured decision required")
        return observation.output

    decision = call(
        DECISION_AGENT,
        "Decide eligibility using intake and policy",
        "Customer Response Agent",
        {"intake": intake, "policy": policy},
        decide,
    )
    call(
        "Customer Response Agent",
        "Communicate the eligibility decision to the customer",
        None,
        decision,
        CustomerResponseAgent().invoke,
    )
    return CanonicalTrace(
        source="controlled.refund",
        task="Process the synthetic customer's refund request using the retrieved company policy.",
        steps=tuple(steps),
    )
