import json
from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.core.context import ContextPolicy
from app.core.exceptions import LLMOutputError
from app.schemas.attributions import LLMResult, ProviderUsage
from app.schemas.regression_specs import (
    Assertion,
    Citation,
    RegressionResult,
    RegressionSpec,
    Scenario,
    SourceReference,
)
from app.schemas.validations import ExecutionResult
from app.services.attribution_input import build_attribution_request
from app.services.context import ContextManager
from app.services.pytest_compiler import PytestCompiler
from app.services.regression_input import build_regression_request, load_evidence
from app.services.regression_validation import validate_regression
from evaluation.artifacts import CallLimit, Experiment
from evaluation.records import attribution_record, regression_record
from evaluation.regressions import evaluate_scenario
from evaluation.sandbox import SuiteDockerRunner
from evaluation.scenarios import controlled_suite, suite_manifest
from tests.unit.test_evaluation_data import prediction


def known_diagnosis(scenario):
    return LLMResult(
        attribution=prediction(scenario.target, 1), usage=ProviderUsage(input_tokens=10)
    )


def known_spec(scenario):
    """Private test fixture, never used as an experiment prediction or provider input."""
    oracle = scenario.oracle
    trace = scenario.execute()
    failure = Citation(
        sequence=1,
        field=oracle.observable,
        quote=json.dumps(
            getattr(trace.steps[1], oracle.observable), sort_keys=True, separators=(",", ":")
        )
        if not isinstance(getattr(trace.steps[1], oracle.observable), str)
        else getattr(trace.steps[1], oracle.observable),
    )
    requirement = Citation(sequence=1, field="task", quote=scenario.task)
    return RegressionSpec(
        version="regression-spec-v1",
        disposition="synthesizable",
        test_name="test_" + scenario.sample_id.replace("-", "_"),
        title="Recorded requirement holds",
        target_agent=scenario.target,
        failure_summary="The target violated the recorded task.",
        scenario=Scenario(
            stimulus=SourceReference(sequence=1, field="input", path=()),
            preconditions=(SourceReference(sequence=0, field="output", path=()),),
        ),
        assertions=(
            Assertion(
                kind=oracle.kind,
                observable=oracle.observable,
                path=oracle.path,
                expected=oracle.expected,
                evidence=(failure, requirement),
            ),
        ),
        evidence=(failure,),
        review_reason=None,
    )


def execution(variant, outcome):
    return ExecutionResult(
        variant=variant,
        outcome=outcome,
        exit_code=0 if outcome == "passed" else 1,
        duration_ms=10,
        passed=int(outcome == "passed"),
        failed=int(outcome == "assertion_failed"),
        diagnostic="PASS" if outcome == "passed" else "REGRESSION_ASSERTION_FAILED",
        container_id="a" * 64,
    )


@pytest.mark.parametrize("scenario", controlled_suite(), ids=lambda s: s.sample_id)
def test_scenarios_execute_actual_variants_and_support_current_compiler(scenario):
    scenario.check_actual_failure()
    faulty, corrected = scenario.execute(), scenario.execute("corrected")
    assert faulty.steps[1].input == corrected.steps[1].input
    assert faulty.steps[0].output == corrected.steps[0].output
    assert faulty.steps[2].input == faulty.steps[1].output
    assert faulty == scenario.execute()
    context = ContextManager(ContextPolicy()).build(faulty)
    attribution = attribution_record(
        scenario.sample_id, known_diagnosis(scenario), context, model="fake-test", effort="medium"
    )
    evidence = load_evidence(attribution, faulty)
    spec = known_spec(scenario)
    snapshot = validate_regression(spec, evidence, max_scenario_bytes=16000)
    record = regression_record(
        scenario.sample_id,
        RegressionResult(spec=spec),
        snapshot,
        model="fake-test",
        effort="medium",
    )
    artifact = PytestCompiler(max_source_bytes=262144, max_scenario_bytes=16000).compile(record)
    assert artifact.source_code
    assert artifact == PytestCompiler(max_source_bytes=262144, max_scenario_bytes=16000).compile(
        record
    )
    # Generated code is inspected here; it is never imported or executed on the host.


@pytest.mark.parametrize("scenario", controlled_suite(), ids=lambda s: s.sample_id)
def test_controlled_oracle_cannot_leak_into_either_production_prompt(scenario):
    altered = replace(
        scenario,
        oracle=replace(
            scenario.oracle,
            responsible_agent="PRIVATE_ORACLE_AGENT",
            decisive_sequence=123456,
            expected="PRIVATE_CORRECTED_ANSWER",
        ),
    )
    assert scenario.execute() == altered.execute()
    requests, regressions = [], []
    for item in (scenario, altered):
        trace = item.execute()
        context = ContextManager(ContextPolicy()).build(trace)
        requests.append(
            build_attribution_request(context, trace, model="fake-test", reasoning_effort="medium")
        )
        attribution = attribution_record(
            item.sample_id, known_diagnosis(scenario), context, model="fake-test", effort="medium"
        )
        regressions.append(
            build_regression_request(
                load_evidence(attribution, trace),
                model="fake-test",
                reasoning_effort="medium",
                max_input_bytes=48000,
            )
        )
    assert requests[0] == requests[1]
    assert regressions[0] == regressions[1]
    assert "PRIVATE" not in requests[0].evidence_json + regressions[0].evidence_json


def test_suite_manifest_is_stable_and_contains_no_trace_or_oracle_values():
    assert suite_manifest() == suite_manifest()
    assert len(suite_manifest()["samples"]) == 6
    assert all(
        set(item) == {"sample_id", "input_digest", "oracle_digest"}
        for item in suite_manifest()["samples"]
    )


@pytest.mark.parametrize(
    "faulty,corrected,verdict",
    [
        ("assertion_failed", "passed", "valid"),
        ("passed", "passed", "not_reproduced"),
        ("assertion_failed", "assertion_failed", "not_discriminating"),
    ],
)
def test_pipeline_uses_real_verdict_contract_not_compilation_alone(
    tmp_path, faulty, corrected, verdict
):
    scenario = controlled_suite()[0]
    client, generator, validator = Mock(), Mock(), Mock()
    client.attribute.return_value = known_diagnosis(scenario)
    generator.generate.return_value = RegressionResult(spec=known_spec(scenario))
    validator.identity.return_value = {"test_fake": True}
    validator.validate.return_value = execution("faulty", faulty), execution("corrected", corrected)
    experiment = Experiment(tmp_path / "exp", {}, resume=False)
    row = evaluate_scenario(
        scenario,
        ContextManager(ContextPolicy()),
        client,
        generator,
        validator,
        experiment,
        model="fake-test",
        effort="medium",
        max_calls=2,
    )
    assert row["attribution_success"] and row["synthesizable"] and row["compiled"]
    assert row["verdict"] == verdict
    assert row["end_to_end_success"] == (verdict == "valid")
    assert experiment.calls_started == 2
    # Reconstructing after interruption reuses both model responses and sandbox results.
    repeated = evaluate_scenario(
        scenario,
        ContextManager(ContextPolicy()),
        client,
        generator,
        validator,
        experiment,
        model="fake-test",
        effort="medium",
        max_calls=2,
    )
    assert repeated["verdict"] == verdict
    assert (
        client.attribute.call_count
        == generator.generate.call_count
        == validator.validate.call_count
        == 1
    )


def test_partial_regression_resume_counts_usage_and_does_not_repeat_attribution(tmp_path):
    scenario = controlled_suite()[0]
    client, generator, validator = Mock(), Mock(), Mock()
    client.attribute.return_value = known_diagnosis(scenario)
    generator.generate.return_value = RegressionResult(spec=known_spec(scenario))
    validator.identity.return_value = {"test_fake": True}
    validator.validate.return_value = (
        execution("faulty", "assertion_failed"),
        execution("corrected", "passed"),
    )
    experiment = Experiment(tmp_path / "exp", {}, resume=False)
    with pytest.raises(CallLimit):
        evaluate_scenario(
            scenario,
            ContextManager(ContextPolicy()),
            client,
            generator,
            validator,
            experiment,
            model="fake-test",
            effort="medium",
            max_calls=1,
        )
    assert experiment.usages()[0]["input_tokens"] == 10
    assert experiment.completed(scenario.sample_id) is None
    row = evaluate_scenario(
        scenario,
        ContextManager(ContextPolicy()),
        client,
        generator,
        validator,
        experiment,
        model="fake-test",
        effort="medium",
        max_calls=2,
    )
    assert row["end_to_end_success"]
    assert client.attribute.call_count == 1
    assert generator.generate.call_count == 1


def test_regression_provider_failure_is_recorded_by_stage_and_preserves_diagnosis_score(tmp_path):
    scenario = controlled_suite()[0]
    client, generator = Mock(), Mock()
    client.attribute.return_value = known_diagnosis(scenario)
    generator.generate.side_effect = LLMOutputError(usage=ProviderUsage(input_tokens=123))
    row = evaluate_scenario(
        scenario,
        ContextManager(ContextPolicy()),
        client,
        generator,
        Mock(),
        Experiment(tmp_path / "exp", {}, resume=False),
        model="fake-test",
        effort="medium",
        max_calls=2,
    )
    assert row["failure"]["stage"] == "regression_provider"
    assert row["attribution_success"]
    assert not row["end_to_end_success"]
    assert row["usages"][-1]["input_tokens"] == 123


def test_abstention_stops_synthesis_without_consulting_private_oracle_for_inference(tmp_path):
    scenario = controlled_suite()[0]
    client, generator = Mock(), Mock()
    client.attribute.return_value = LLMResult(attribution=prediction(abstain=True))
    row = evaluate_scenario(
        scenario,
        ContextManager(ContextPolicy()),
        client,
        generator,
        Mock(),
        Experiment(tmp_path / "exp", {}, resume=False),
        model="fake-test",
        effort="medium",
        max_calls=2,
    )
    assert row["abstained"]
    assert not row["attribution_success"]
    generator.generate.assert_not_called()


def test_suite_overlay_keeps_all_production_container_restrictions(settings):
    from app.schemas.validations import RuntimeIdentity, SandboxLimits

    runner = SuiteDockerRunner.__new__(SuiteDockerRunner)
    runner.settings = settings
    runner.base_runtime = RuntimeIdentity(
        image_reference="production:1",
        image_id="sha256:" + "a" * 64,
        bundle_digest="b" * 64,
        limits=SandboxLimits.from_settings(settings),
    )
    runner.suite_image_id = "sha256:" + "c" * 64
    args = runner.create_args(runner.base_runtime, "faulty", uuid4(), uuid4(), "test")
    assert args[-1] == runner.suite_image_id
    assert args[args.index("--network") + 1] == "none"
    assert "--read-only" in args
    assert args[args.index("--user") + 1] == "10001:10001"
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert "--memory" in args and "--cpus" in args and "--pids-limit" in args
    assert not any(
        "API_KEY" in value or "--volume" in value or "--privileged" in value for value in args
    )


@pytest.mark.parametrize("scenario", controlled_suite(), ids=lambda s: s.sample_id)
def test_complete_replay_fixture_passes_guard_for_all_six_scenarios(tmp_path, scenario):
    client, generator, validator = Mock(), Mock(), Mock()
    client.attribute.return_value = known_diagnosis(scenario)
    generator.generate.return_value = RegressionResult(spec=known_spec(scenario))
    validator.identity.return_value = {"offline_fake": True}
    validator.validate.return_value = (
        execution("faulty", "assertion_failed"),
        execution("corrected", "passed"),
    )
    row = evaluate_scenario(
        scenario,
        ContextManager(ContextPolicy()),
        client,
        generator,
        validator,
        Experiment(tmp_path / "exp", {}, resume=False),
        model="fake-test",
        effort="medium",
        max_calls=2,
    )
    assert row["replay_compatible"] and row["end_to_end_success"]


@pytest.mark.parametrize(
    "sample_id", ["incorrect-string", "forbidden-content", "missing-completion"]
)
def test_scalar_setup_is_rejected_before_compilation_or_docker(tmp_path, sample_id, monkeypatch):
    scenario = next(s for s in controlled_suite() if s.sample_id == sample_id)
    spec = known_spec(scenario)
    scalar_scenario = spec.scenario.model_copy(
        update={"preconditions": (SourceReference(sequence=0, field="output", path=("mode",)),)}
    )
    spec = spec.model_copy(update={"scenario": scalar_scenario})
    original = spec.model_dump_json()
    client, generator, validator = Mock(), Mock(), Mock()
    client.attribute.return_value = known_diagnosis(scenario)
    generator.generate.return_value = RegressionResult(spec=spec)
    compile_call = Mock(side_effect=AssertionError("Incompatible fixture reached compiler"))
    monkeypatch.setattr(PytestCompiler, "compile", compile_call)
    row = evaluate_scenario(
        scenario,
        ContextManager(ContextPolicy()),
        client,
        generator,
        validator,
        Experiment(tmp_path / "exp", {}, resume=False),
        model="fake-test",
        effort="medium",
        max_calls=2,
    )
    assert row["failure"]["code"] == "REPLAY_CONTRACT_INCOMPATIBLE"
    assert row["failure"]["stage"] == "replay_contract"
    assert row["synthesizable"] and not row["replay_compatible"] and not row["compiled"]
    assert row["verdict"] is None and not row["end_to_end_success"]
    validator.validate.assert_not_called()
    compile_call.assert_not_called()
    assert spec.model_dump_json() == original
