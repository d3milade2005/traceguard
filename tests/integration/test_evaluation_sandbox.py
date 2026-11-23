"""Opt-in real Docker harness verification. No provider calls or benchmark predictions."""

import os
from uuid import uuid4

import pytest

from app.core.context import ContextPolicy
from app.integrations.execution.contracts import ExecutionBundle
from app.schemas.regression_specs import RegressionResult
from app.schemas.validations import classify
from app.services.context import ContextManager
from app.services.pytest_compiler import PytestCompiler
from app.services.regression_input import load_evidence
from app.services.regression_validation import validate_regression
from evaluation.records import attribution_record, regression_record
from evaluation.sandbox import SuiteDockerRunner
from evaluation.scenarios import controlled_suite
from scripts.evaluate_regressions import sandbox_settings
from tests.unit.test_evaluation_regressions import known_diagnosis, known_spec

pytestmark = [pytest.mark.integration, pytest.mark.sandbox]


@pytest.mark.parametrize("scenario", controlled_suite(), ids=lambda s: s.sample_id)
def test_suite_actual_faulty_assertion_failure_and_corrected_pass(scenario):
    if os.environ.get("RUN_EVALUATION_SANDBOX_TESTS") != "true":
        pytest.skip("Set RUN_EVALUATION_SANDBOX_TESTS=true with the verified evaluation image")
    runner = SuiteDockerRunner(
        sandbox_settings(),
        base_image="traceguard-validation:1",
        suite_image="traceguard-evaluation:2",
    )
    trace = scenario.execute()
    attribution = attribution_record(
        scenario.sample_id,
        known_diagnosis(scenario),
        ContextManager(ContextPolicy()).build(trace),
        model="offline-fixture",
        effort="medium",
    )
    spec = known_spec(scenario)
    snapshot = validate_regression(
        spec, load_evidence(attribution, trace), max_scenario_bytes=16000
    )
    record = regression_record(
        scenario.sample_id,
        RegressionResult(spec=spec),
        snapshot,
        model="offline-fixture",
        effort="medium",
    )
    compiled = PytestCompiler(max_source_bytes=262144, max_scenario_bytes=16000).compile(record)
    faulty, corrected = runner.validate(
        ExecutionBundle(compiled.source_code, compiled.test_name), uuid4()
    )
    assert faulty.outcome == "assertion_failed"
    assert corrected.outcome == "passed"
    assert classify(faulty, corrected) == "valid"
