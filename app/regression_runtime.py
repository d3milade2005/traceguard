"""Approved harness and assertion API for future isolated generated-test execution."""

import json
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, JsonValue

HARNESS_ID = "traceguard.agent-invocation"
HARNESS_VERSION = "1"
AssertionKind = Literal["equals", "not_equals", "contains", "not_contains", "exists", "not_exists"]
Scalar = str | bool | int | float | None


class RegressionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    output: JsonValue = None
    tool_result: JsonValue = None
    status: str | None = None


class RegressionTestHarness(Protocol):
    identifier: str
    version: str

    def invoke(
        self, *, target_agent: str, stimulus: JsonValue, preconditions: tuple[JsonValue, ...]
    ) -> RegressionObservation: ...


def require_harness(
    harness: RegressionTestHarness, identifier: str, version: str
) -> RegressionTestHarness:
    if identifier != HARNESS_ID or version != HARNESS_VERSION:
        raise ValueError("Unsupported harness contract")
    if harness.identifier != identifier or harness.version != version:
        raise ValueError("Harness identity or version does not match the artifact")
    return harness


def require_observation(observation: RegressionObservation) -> RegressionObservation:
    if type(observation) is not RegressionObservation:
        raise ValueError("Harness must return RegressionObservation")
    return RegressionObservation.model_validate_json(
        json.dumps(observation.model_dump(mode="json"), allow_nan=False)
    )


def resolve(value: JsonValue, path: tuple[str | int, ...]) -> tuple[bool, JsonValue]:
    """Traverse built-in JSON containers only; null is present, attributes are inaccessible."""
    for part in path:
        if type(part) is str and type(value) is dict and part in value:
            value = value.get(part)
        elif type(part) is int and type(value) is list and 0 <= part < len(value):
            value = value[part]
        else:
            return False, None
    return True, value


def assertion_holds(
    kind: AssertionKind, value: JsonValue, path: tuple[str | int, ...], expected: Scalar
) -> bool:
    present, actual = resolve(value, path)
    if kind == "exists":
        return present
    if kind == "not_exists":
        return not present
    if not present:
        return False
    if kind in {"contains", "not_contains"}:
        if type(actual) is not str or type(expected) is not str:
            raise ValueError("Containment requires string observations and operands")
        contains = expected in actual
        return contains if kind == "contains" else not contains
    equal = type(actual) is type(expected) and actual == expected
    if kind == "equals":
        return equal
    if kind == "not_equals":
        return not equal
    raise ValueError("Unsupported assertion kind")


def _assert(
    kind: AssertionKind, value: JsonValue, path: tuple[str | int, ...], expected: Scalar
) -> None:
    # Explicit raise retains checks even when Python is launched with optimization.
    if not assertion_holds(kind, value, path, expected):
        raise AssertionError(f"Regression assertion {kind} failed at path {path!r}")


def assert_equals(value: JsonValue, path: tuple[str | int, ...], expected: Scalar) -> None:
    _assert("equals", value, path, expected)


def assert_not_equals(value: JsonValue, path: tuple[str | int, ...], expected: Scalar) -> None:
    _assert("not_equals", value, path, expected)


def assert_contains(value: JsonValue, path: tuple[str | int, ...], expected: Scalar) -> None:
    _assert("contains", value, path, expected)


def assert_not_contains(value: JsonValue, path: tuple[str | int, ...], expected: Scalar) -> None:
    _assert("not_contains", value, path, expected)


def assert_exists(value: JsonValue, path: tuple[str | int, ...]) -> None:
    _assert("exists", value, path, None)


def assert_not_exists(value: JsonValue, path: tuple[str | int, ...]) -> None:
    _assert("not_exists", value, path, None)
