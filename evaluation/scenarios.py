"""Track B: actual executions and private scoring oracles are separate projections."""

from dataclasses import dataclass

from pydantic import JsonValue

from app.regression_runtime import AssertionKind, Scalar, assertion_holds
from app.schemas.traces import CanonicalTrace, TraceStep
from app.schemas.validations import Variant
from evaluation.artifacts import digest
from evaluation.harness import SuiteHarness

SUITE_VERSION = "controlled-suite-v1"


@dataclass(frozen=True)
class Oracle:
    responsible_agent: str
    decisive_sequence: int
    kind: AssertionKind
    observable: str
    path: tuple[str | int, ...]
    expected: Scalar


@dataclass(frozen=True)
class ControlledScenario:
    sample_id: str
    target: str
    stimulus: dict[str, JsonValue]
    setup: dict[str, JsonValue]
    task: str
    oracle: Oracle

    def execute(self, variant: Variant = "faulty") -> CanonicalTrace:
        observation = SuiteHarness(faulty=variant == "faulty").invoke(
            target_agent=self.target, stimulus=self.stimulus, preconditions=(self.setup,)
        )
        # The reporter really forwards the target observation, without repairing it.
        return CanonicalTrace(
            source="evaluation.controlled",
            task=self.task,
            steps=(
                TraceStep(
                    sequence=0,
                    agent_id="coordinator",
                    event_type="setup",
                    output=self.setup,
                    status="success",
                ),
                TraceStep(
                    sequence=1,
                    agent_id=self.target,
                    event_type="invocation",
                    input=self.stimulus,
                    output=observation.output,
                    status=observation.status,
                ),
                TraceStep(
                    sequence=2,
                    agent_id="reporter",
                    event_type="report",
                    input=observation.output,
                    output={
                        "reported": observation.output,
                        "invocation_status": observation.status,
                    },
                    status="success",
                ),
            ),
        )

    def ground_truth(self) -> dict:
        from dataclasses import asdict

        return asdict(self.oracle)

    def check_actual_failure(self) -> None:
        for variant, expected in (("faulty", False), ("corrected", True)):
            trace = self.execute(variant)
            step = trace.steps[self.oracle.decisive_sequence]
            actual = assertion_holds(
                self.oracle.kind,
                getattr(step, self.oracle.observable),
                self.oracle.path,
                self.oracle.expected,
            )
            if step.agent_id != self.oracle.responsible_agent or actual != expected:
                raise ValueError("Controlled implementation no longer matches its private oracle")


def controlled_suite() -> tuple[ControlledScenario, ...]:
    public: dict[str, JsonValue] = {"mode": "public"}
    return (
        ControlledScenario(
            "arithmetic",
            "calculator",
            {"left": 2, "right": 2},
            {"operation": "add"},
            "Add 2 and 2; the sum must equal 4. Coordinator setup is required precondition state. "
            "The reporter forwards the result without changing it.",
            Oracle("calculator", 1, "equals", "output", ("sum",), 4),
        ),
        ControlledScenario(
            "missing-field",
            "profile_builder",
            {"name": "Ada"},
            public,
            "Build a public profile for the supplied name. Output must include the active field. "
            "Coordinator mode is required precondition state. Reporter forwards the profile.",
            Oracle("profile_builder", 1, "exists", "output", ("active",), None),
        ),
        ControlledScenario(
            "unexpected-field",
            "public_formatter",
            {"name": "Ada"},
            public,
            "Format a public profile. Output must exclude the internal_note field. "
            "Coordinator mode is required precondition state. Reporter forwards the profile.",
            Oracle("public_formatter", 1, "not_exists", "output", ("internal_note",), None),
        ),
        ControlledScenario(
            "incorrect-string",
            "greeter",
            {"name": "Ada"},
            public,
            "Greet Ada. The output message must contain Hello. "
            "Coordinator mode is required precondition state. Reporter forwards the message.",
            Oracle("greeter", 1, "contains", "output", ("message",), "Hello"),
        ),
        ControlledScenario(
            "forbidden-content",
            "redactor",
            {"name": "Ada", "text": "Ada SECRET"},
            public,
            "Redact the supplied text. The output message must not contain SECRET. "
            "Coordinator mode is required precondition state. Reporter forwards the message.",
            Oracle("redactor", 1, "not_contains", "output", ("message",), "SECRET"),
        ),
        ControlledScenario(
            "missing-completion",
            "completion_agent",
            {"name": "Ada"},
            public,
            "Complete the supplied profile task. Invocation status must equal complete. "
            "Coordinator mode is required precondition state. Reporter forwards the observation.",
            Oracle("completion_agent", 1, "equals", "status", (), "complete"),
        ),
    )


def suite_manifest() -> dict:
    scenarios = controlled_suite()
    return {
        "version": SUITE_VERSION,
        "samples": [
            {
                "sample_id": s.sample_id,
                "input_digest": digest(s.execute().model_dump(mode="json")),
                "oracle_digest": digest(s.ground_truth()),
            }
            for s in scenarios
        ],
    }
