import pytest
from pydantic import ValidationError

from app import regression_runtime as runtime
from app.services.regression_validation import assertion_holds as phase5_holds
from tests.fixtures.replayable import ControlledCalculatorHarness, calculator_record


@pytest.mark.parametrize(
    "kind,value,path,expected,holds",
    [
        ("equals", 4, (), 4, True),
        ("equals", 4.0, (), 4, False),
        ("equals", True, (), 1, False),
        ("equals", False, (), 0, False),
        ("equals", None, (), None, True),
        ("equals", {"a": None}, ("a",), None, True),
        ("equals", {}, ("a",), None, False),
        ("not_equals", {}, ("a",), 4, False),
        ("not_equals", None, (), 4, True),
        ("not_equals", 4.0, (), 4, True),
        ("contains", "Answer 4", (), "4", True),
        ("contains", "Answer 4", (), "9", False),
        ("not_contains", "Answer 4", (), "9", True),
        ("not_contains", {}, ("missing",), "9", False),
        ("exists", {"a": None}, ("a",), None, True),
        ("not_exists", {"a": None}, ("a",), None, False),
        ("not_exists", {"a": None}, ("missing",), None, True),
        ("equals", {"a": [1, {"b": 4}]}, ("a", 1, "b"), 4, True),
    ],
)
def test_assertion_semantics_match_the_phase5_contract(kind, value, path, expected, holds):
    assert runtime.assertion_holds(kind, value, path, expected) is holds
    assertion = (
        calculator_record()
        .spec.assertions[0]
        .model_copy(update={"kind": kind, "path": path, "expected": expected})
    )
    assert phase5_holds(assertion, value) is holds
    helper = getattr(runtime, "assert_" + kind)
    operands = (value, path) if kind in {"exists", "not_exists"} else (value, path, expected)
    if holds:
        helper(*operands)
    else:
        with pytest.raises(AssertionError):
            helper(*operands)


@pytest.mark.parametrize("kind", ["contains", "not_contains"])
@pytest.mark.parametrize("value", [4, None, [], {}])
def test_containment_on_nonstring_is_an_invalid_observation(kind, value):
    with pytest.raises(ValueError):
        runtime.assertion_holds(kind, value, (), "4")


@pytest.mark.parametrize(
    "path", [(True,), (-1,), ("a.b",), ("a[0]",), ("__class__",), ("get()",), (2,)]
)
def test_resolver_never_interprets_attribute_or_expression_paths(path):
    assert runtime.resolve({"a": {"b": 1}}, path) == (False, None)
    assert runtime.resolve([1, 2], path) == (False, None)


def test_literal_keys_and_indexes_and_present_null():
    assert runtime.resolve({"a": [1, {"b": None}]}, ("a", 1, "b")) == (True, None)
    assert runtime.resolve([0, 1], (1,)) == (True, 1)
    assert runtime.resolve(None, ()) == (True, None)
    assert runtime.resolve({"literal": 4}, ("literal",)) == (True, 4)


def test_resolver_ignores_custom_lookup_and_descriptor_execution():
    class EvilDict(dict):
        def get(self, key):
            raise AssertionError("Custom lookup ran")

    class EvilObject:
        @property
        def data(self):
            raise AssertionError("Descriptor ran")

    assert runtime.resolve(EvilDict(a=1), ("a",)) == (False, None)
    assert runtime.resolve(EvilObject(), ("data",)) == (False, None)


def test_observation_contract_rejects_arbitrary_results_extra_fields_and_nonfinite_values():
    assert runtime.require_observation(
        runtime.RegressionObservation()
    ) == runtime.RegressionObservation(output=None, tool_result=None, status=None)
    with pytest.raises(ValueError):
        runtime.require_observation({"output": 4})
    with pytest.raises(ValueError):
        runtime.require_observation(runtime.RegressionObservation(output=float("nan")))
    with pytest.raises(ValidationError):
        runtime.RegressionObservation(status=True)
    with pytest.raises(ValidationError):
        runtime.RegressionObservation(output=4, callback="anything")


def test_developer_owned_replayable_harness_contract_without_generated_test_execution():
    harness = ControlledCalculatorHarness()
    assert runtime.require_harness(harness, runtime.HARNESS_ID, runtime.HARNESS_VERSION) is harness
    observation = harness.invoke(
        target_agent="calculator",
        stimulus={"left": 2, "right": 2},
        preconditions=({"operation": "add"},),
    )
    assert runtime.require_observation(observation).output == {"sum": 5}
    for identifier, version in (("arbitrary_module", "1"), (runtime.HARNESS_ID, "999")):
        with pytest.raises(ValueError):
            runtime.require_harness(harness, identifier, version)
    harness.version = "999"
    with pytest.raises(ValueError):
        runtime.require_harness(harness, runtime.HARNESS_ID, runtime.HARNESS_VERSION)
    with pytest.raises(ValueError):
        harness.invoke(target_agent="arbitrary_module", stimulus={}, preconditions=())
