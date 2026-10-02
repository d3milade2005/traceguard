"""Developer-owned, replayable arithmetic fixture, separate from attribution benchmarks."""

from datetime import UTC, datetime
from uuid import UUID

from pydantic import JsonValue

from app.regression_runtime import HARNESS_ID, HARNESS_VERSION, RegressionObservation
from app.schemas.regression_specs import (
    Assertion,
    Citation,
    RegressionRecord,
    RegressionSpec,
    Scenario,
    ScenarioSnapshot,
    SourceReference,
)
from app.schemas.traces import CanonicalTrace, TraceStep


def calculator_trace() -> CanonicalTrace:
    return CanonicalTrace(
        source="controlled.calculator",
        task="Add the two inputs; the sum must equal 4.",
        steps=(
            TraceStep(
                sequence=0, agent_id="coordinator", event_type="setup", output={"operation": "add"}
            ),
            TraceStep(
                sequence=1,
                agent_id="calculator",
                event_type="invocation",
                input={"left": 2, "right": 2},
                output={"sum": 5},
                status="success",
            ),
        ),
    )


def calculator_spec() -> RegressionSpec:
    failure = Citation(sequence=1, field="output", quote="5")
    requirement = Citation(sequence=1, field="task", quote="the sum must equal 4")
    return RegressionSpec(
        version="regression-spec-v1",
        disposition="synthesizable",
        test_name="test_calculator_returns_expected_sum",
        title="Calculator returns the required sum",
        target_agent="calculator",
        failure_summary="The calculator returned an incorrect sum.",
        scenario=Scenario(
            stimulus=SourceReference(sequence=1, field="input", path=()),
            preconditions=(SourceReference(sequence=0, field="output", path=()),),
        ),
        assertions=(
            Assertion(
                kind="equals",
                observable="output",
                path=("sum",),
                expected=4,
                evidence=(failure, requirement),
            ),
        ),
        evidence=(failure,),
        review_reason=None,
    )


def calculator_record() -> RegressionRecord:
    spec = calculator_spec()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return RegressionRecord(
        id=UUID(int=1),
        attribution_id=UUID(int=2),
        status="completed",
        disposition=spec.disposition,
        version=spec.version,
        title=spec.title,
        target_agent=spec.target_agent,
        review_reason=None,
        spec=spec,
        scenario_snapshot=ScenarioSnapshot(
            stimulus={"left": 2, "right": 2}, preconditions=({"operation": "add"},)
        ),
        provider="offline_fixture",
        model="none",
        reasoning_effort="medium",
        prompt_version=spec.version,
        response_id=None,
        response_model=None,
        input_tokens=None,
        output_tokens=None,
        reasoning_tokens=None,
        latency_ms=None,
        input_sha256=None,
        failure_code=None,
        failed_at=None,
        completed_at=now,
        created_at=now,
        updated_at=now,
    )


class ControlledCalculatorHarness:
    """One known faulty target; no generated module loading or corrected target comparison."""

    identifier = HARNESS_ID
    version = HARNESS_VERSION

    def invoke(
        self, *, target_agent: str, stimulus: JsonValue, preconditions: tuple[JsonValue, ...]
    ) -> RegressionObservation:
        if target_agent != "calculator" or preconditions != ({"operation": "add"},):
            raise ValueError("Unsupported controlled scenario")
        if not isinstance(stimulus, dict):
            raise ValueError("Structured arithmetic input required")
        left, right = stimulus.get("left"), stimulus.get("right")
        if type(left) is not int or type(right) is not int:
            raise ValueError("Integer operands required")
        return RegressionObservation(output={"sum": left + right + 1}, status="success")
