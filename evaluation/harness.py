"""Developer-owned deterministic agents; this module is also copied into the sandbox.

No benchmark labels, scenario oracles, generated code, or provider client is imported here.
"""

from pydantic import JsonValue

from app.integrations.execution.calculator import CorrectedCalculator, FaultyCalculator, operands
from app.regression_runtime import HARNESS_ID, HARNESS_VERSION, RegressionObservation

REPLAY_CONTRACT_VERSION = "controlled-suite-replay-v1"


def validate_invocation(
    *, target_agent: str, stimulus: JsonValue, preconditions: tuple[JsonValue, ...]
) -> None:
    """Check the same developer-owned input contract without executing either variant."""
    if target_agent == "calculator":
        operands(target_agent, stimulus, preconditions)
        return
    if target_agent not in {
        "profile_builder",
        "public_formatter",
        "greeter",
        "redactor",
        "completion_agent",
    }:
        raise ValueError("Unsupported suite agent")
    if preconditions != ({"mode": "public"},) or not isinstance(stimulus, dict):
        raise ValueError("Unsupported suite setup or stimulus")
    name = stimulus.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("A nonempty name is required")
    if target_agent == "redactor" and not isinstance(stimulus.get("text"), str):
        raise ValueError("Redactor requires text")


class SuiteHarness:
    identifier = HARNESS_ID
    version = HARNESS_VERSION

    def __init__(self, *, faulty: bool) -> None:
        self.faulty = faulty

    def invoke(
        self, *, target_agent: str, stimulus: JsonValue, preconditions: tuple[JsonValue, ...]
    ) -> RegressionObservation:
        validate_invocation(
            target_agent=target_agent, stimulus=stimulus, preconditions=preconditions
        )
        if target_agent == "calculator":
            calculator = FaultyCalculator() if self.faulty else CorrectedCalculator()
            return calculator.invoke(
                target_agent=target_agent, stimulus=stimulus, preconditions=preconditions
            )
        assert isinstance(stimulus, dict)
        name = stimulus.get("name")
        assert isinstance(name, str)
        output: JsonValue
        if target_agent == "profile_builder":
            output = {"name": name} if self.faulty else {"name": name, "active": True}
        elif target_agent == "public_formatter":
            output = {"name": name, "internal_note": "private"} if self.faulty else {"name": name}
        elif target_agent == "greeter":
            output = {"message": "Goodbye " + name} if self.faulty else {"message": "Hello " + name}
        elif target_agent == "redactor":
            text = stimulus.get("text")
            assert isinstance(text, str)
            output = {"message": text if self.faulty else text.replace("SECRET", "[redacted]")}
        elif target_agent == "completion_agent":
            return RegressionObservation(
                output={"name": name}, status="pending" if self.faulty else "complete"
            )
        else:
            raise ValueError("Unsupported suite agent")
        return RegressionObservation(output=output, status="success")
