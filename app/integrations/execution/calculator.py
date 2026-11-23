"""Approved controlled replay system; unrelated to Who&When benchmark traces."""

from pydantic import JsonValue

from app.regression_runtime import HARNESS_ID, HARNESS_VERSION, RegressionObservation


class FaultyCalculator:
    identifier = HARNESS_ID
    version = HARNESS_VERSION

    def invoke(
        self, *, target_agent: str, stimulus: JsonValue, preconditions: tuple[JsonValue, ...]
    ) -> RegressionObservation:
        left, right = operands(target_agent, stimulus, preconditions)
        return RegressionObservation(output={"sum": left + right + 1}, status="success")


class CorrectedCalculator:
    identifier = HARNESS_ID
    version = HARNESS_VERSION

    def invoke(
        self, *, target_agent: str, stimulus: JsonValue, preconditions: tuple[JsonValue, ...]
    ) -> RegressionObservation:
        left, right = operands(target_agent, stimulus, preconditions)
        return RegressionObservation(output={"sum": left + right}, status="success")


def operands(
    target: str, stimulus: JsonValue, preconditions: tuple[JsonValue, ...]
) -> tuple[int, int]:
    if (
        target != "calculator"
        or preconditions != ({"operation": "add"},)
        or not isinstance(stimulus, dict)
    ):
        raise ValueError("Unsupported controlled calculator scenario")
    left, right = stimulus.get("left"), stimulus.get("right")
    if type(left) is not int or type(right) is not int:
        raise ValueError("Integer operands required")
    return left, right
