"""Deterministic coordinator/calculator/reporter agents with an external failure check.

The calculator is exactly the implementation replayed by the Docker harness.
Only developer-owned agent code executes here; generated tests never do.
"""

from pydantic import JsonValue

from app.integrations.execution.calculator import CorrectedCalculator, FaultyCalculator
from app.schemas.traces import CanonicalTrace, TraceStep
from app.schemas.validations import Variant


class CoordinatorAgent:
    def plan(self, left: int, right: int) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
        return {"operation": "add"}, {"left": left, "right": right}


class ReporterAgent:
    def report(self, observation: JsonValue) -> JsonValue:
        if not isinstance(observation, dict) or type(observation.get("sum")) is not int:
            raise ValueError("Calculator must return an integer sum")
        return {"answer": observation["sum"]}


def execute_calculator_system(variant: Variant = "faulty") -> CanonicalTrace:
    setup, stimulus = CoordinatorAgent().plan(2, 2)
    calculator = FaultyCalculator() if variant == "faulty" else CorrectedCalculator()
    observation = calculator.invoke(
        target_agent="calculator", stimulus=stimulus, preconditions=(setup,)
    )
    reported = ReporterAgent().report(observation.output)
    return CanonicalTrace(
        source="controlled.calculator",
        task=(
            "Add 2 and 2; the sum must equal 4. The coordinator establishes the operation "
            "as calculator precondition state. The calculator receives the operands and returns "
            "a sum. The reporter forwards that result without changing it."
        ),
        steps=(
            TraceStep(
                sequence=0,
                agent_id="coordinator",
                event_type="setup",
                output=setup,
                status="success",
            ),
            TraceStep(
                sequence=1,
                agent_id="calculator",
                event_type="invocation",
                input=stimulus,
                output=observation.output,
                status=observation.status,
            ),
            TraceStep(
                sequence=2,
                agent_id="reporter",
                event_type="report",
                input=observation.output,
                output=reported,
                status="success",
            ),
        ),
    )
