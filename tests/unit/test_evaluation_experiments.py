import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from app.core.context import ContextPolicy
from app.core.exceptions import LLMOutputError
from app.schemas.attributions import AttributionRequest, LLMResult, ProviderUsage
from app.services.context import ContextManager
from evaluation.artifacts import (
    CallLimit,
    Experiment,
    RecordedCallFailure,
    UncertainCall,
    read_json,
    write_once,
    write_report,
)
from evaluation.attribution import evaluate_sample
from scripts import evaluate_attribution, evaluate_regressions, freeze_evaluation
from scripts.compare_evaluations import compare
from tests.unit.test_evaluation_data import prediction, raw_record, sample


def request(model="test"):
    return AttributionRequest(
        prompt_version="test-v1",
        instructions="test",
        evidence_json="{}",
        model=model,
        reasoning_effort="medium",
    )


def result():
    return LLMResult(
        attribution=prediction(),
        usage=ProviderUsage(input_tokens=10, output_tokens=5, reasoning_tokens=1, latency_ms=20),
    )


def test_incremental_resume_and_call_limits_do_not_repeat_paid_calls(tmp_path):
    root = tmp_path / "experiment"
    experiment = Experiment(root, {"test": 1}, resume=False)
    invoke = Mock(return_value=result())
    with experiment.locked():
        response = experiment.call("a", "attribution", request(), LLMResult, invoke, max_calls=1)
        assert response == result()
    resumed = Experiment(root, {"test": 1}, resume=True)
    with resumed.locked():
        assert (
            resumed.call("a", "attribution", request(), LLMResult, invoke, max_calls=1) == result()
        )
        with pytest.raises(CallLimit):
            resumed.call("b", "attribution", request(), LLMResult, invoke, max_calls=1)
        resumed.save("a", {"prediction": response.attribution.model_dump(mode="json")})
    assert invoke.call_count == 1
    assert resumed.calls_started == 1
    assert resumed.completed("a")["experiment_id"] == experiment.id
    assert resumed.usages()[0]["input_tokens"] == 10
    assert len(resumed.results()) == 1
    with pytest.raises(FileExistsError):
        resumed.save("a", {})


def test_pending_call_intent_never_retries_after_uncertain_crash(tmp_path):
    experiment = Experiment(tmp_path / "exp", {}, resume=False)
    crashed = Mock(side_effect=KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        experiment.call("a", "attribution", request(), LLMResult, crashed, max_calls=2)
    with pytest.raises(UncertainCall):
        experiment.call("a", "attribution", request(), LLMResult, crashed, max_calls=2)
    assert crashed.call_count == 1
    assert experiment.calls_started == 1


def test_terminal_provider_errors_preserve_usage_without_secret_exception_text(tmp_path, caplog):
    experiment = Experiment(tmp_path / "exp", {}, resume=False)
    invoke = Mock(side_effect=LLMOutputError(usage=ProviderUsage(input_tokens=20)))
    for _ in range(2):
        with pytest.raises(RecordedCallFailure):
            experiment.call("a", "attribution", request(), LLMResult, invoke, max_calls=4)
    assert invoke.call_count == 1
    assert experiment.usages()[0]["input_tokens"] == 20
    assert experiment.calls_started == 1


def test_no_overwrite_changed_config_changed_request_and_lock_safety(tmp_path):
    root = tmp_path / "exp"
    experiment = Experiment(root, {"model": "test"}, resume=False)
    with pytest.raises(ValueError, match="exists"):
        Experiment(root, {"model": "test"}, resume=False)
    with pytest.raises(ValueError, match="differs"):
        Experiment(root, {"model": "other"}, resume=True)
    with experiment.locked(), pytest.raises(FileExistsError), experiment.locked():
        pass
    assert not (root / ".lock").exists()
    experiment.call("a", "attribution", request(), LLMResult, result, max_calls=1)
    with pytest.raises(ValueError, match="input differs"):
        experiment.call("a", "attribution", request("other"), LLMResult, result, max_calls=2)


def test_zero_call_limit_cannot_invoke_provider(tmp_path):
    experiment = Experiment(tmp_path / "exp", {}, resume=False)
    invoke = Mock()
    with pytest.raises(CallLimit):
        experiment.call("a", "attribution", request(), LLMResult, invoke, max_calls=0)
    invoke.assert_not_called()
    assert experiment.calls_started == 0


def test_scoring_reads_ground_truth_only_after_prediction(tmp_path):
    state = {"predicted": False}

    class GuardedRecord(dict):
        def get(self, key, default=None):
            if key.startswith("mistake_") or key in {"ground_truth", "question_ID"}:
                assert state["predicted"], "Ground truth was read before prediction"
            return super().get(key, default)

    from evaluation.who_when import Sample

    item = Sample("guarded", "Algorithm-Generated", 0, GuardedRecord(raw_record()))
    client = Mock()

    def attribute(req):
        assert "PRIVATE" not in req.evidence_json
        assert "mistake_" not in req.evidence_json
        state["predicted"] = True
        return result()

    client.attribute.side_effect = attribute
    row = evaluate_sample(
        item,
        ContextManager(ContextPolicy()),
        client,
        Experiment(tmp_path / "exp", {}, resume=False),
        model="test",
        effort="medium",
        max_calls=1,
    )
    assert row["scores"]["joint_correct"] is True
    assert row["failure"] is None


def test_wrong_predictions_are_terminal_data_not_retry_triggers(tmp_path):
    client = Mock()
    client.attribute.return_value = LLMResult(attribution=prediction("Manager", 0))
    experiment = Experiment(tmp_path / "exp", {}, resume=False)
    row = evaluate_sample(
        sample(),
        ContextManager(ContextPolicy()),
        client,
        experiment,
        model="test",
        effort="medium",
        max_calls=10,
    )
    experiment.save("sample", row)
    assert row["scores"]["joint_correct"] is False
    assert row["failure"] is None
    assert client.attribute.call_count == 1


def test_semantically_invalid_output_is_preserved_and_not_scored_as_covered(tmp_path):
    client = Mock()
    client.attribute.return_value = LLMResult(attribution=prediction("Unknown", 100))
    experiment = Experiment(tmp_path / "exp", {}, resume=False)
    row = evaluate_sample(
        sample(),
        ContextManager(ContextPolicy()),
        client,
        experiment,
        model="test",
        effort="medium",
        max_calls=1,
    )
    assert row["prediction"]["decisive_sequence"] == 100
    assert row["invalid_semantic_output"]
    assert not row["scores"]["covered"]
    assert row["failure"]["stage"] == "attribution_validation"
    assert row["usage"] is not None


def test_report_serialization_and_repeat_resume_are_deterministic(tmp_path):
    report = {"track": "test", "metrics": {"total": 2}, "usage": {"input_tokens": None}}
    write_report(tmp_path, report)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    write_report(tmp_path, deepcopy(report))
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert json.loads(next(tmp_path.glob("*.json")).read_text()) == report


def test_attribution_cli_defaults_offline_and_never_marks_dry_run_predictions(
    tmp_path, monkeypatch
):
    from evaluation.who_when import audit_dataset, build_manifests

    items = [sample()]
    manifest = build_manifests(
        items,
        audit_dataset(items, ContextManager(ContextPolicy())),
        fixture_dir=tmp_path,
        development_count=1,
    )
    path = tmp_path / "manifest.json"
    write_once(path, manifest)
    monkeypatch.setattr(evaluate_attribution, "load_dataset", lambda _: items)
    provider = Mock(side_effect=AssertionError("Provider must not be constructed"))
    monkeypatch.setattr(evaluate_attribution, "OpenAILLMClient", provider)
    monkeypatch.setattr(
        "sys.argv", ["evaluate", "--manifest", str(path), "--output", str(tmp_path / "out")]
    )
    evaluate_attribution.main()
    assert not (tmp_path / "out/results").exists()
    assert not (tmp_path / "out/calls").exists()
    plan = read_json(next((tmp_path / "out").glob("report-*.json")))
    assert plan["sample_count"] == plan["remaining_samples"] == 1
    assert plan["calls_already_started"] == plan["already_completed_samples"] == 0
    assert plan["prompt_version"] == "attribution-v1" and plan["manifest_digest"]
    provider.assert_not_called()


def test_regression_cli_defaults_offline_and_checks_real_variants(tmp_path, monkeypatch):
    provider = Mock(side_effect=AssertionError("No provider in dry run"))
    validator = Mock(side_effect=AssertionError("No Docker in dry run"))
    monkeypatch.setattr(evaluate_regressions, "OpenAILLMClient", provider)
    monkeypatch.setattr(evaluate_regressions, "SuiteDockerRunner", validator)
    monkeypatch.setattr("sys.argv", ["evaluate", "--output", str(tmp_path / "out")])
    evaluate_regressions.main()
    manifest = read_json(tmp_path / "out/suite-manifest.json")
    assert len(manifest["samples"]) == 6
    provider.assert_not_called()
    validator.assert_not_called()


def test_sandbox_configuration_ignores_unrelated_host_settings(monkeypatch):
    monkeypatch.setenv("DEBUG", "not-a-boolean")
    monkeypatch.setenv("OPENAI_API_KEY", "test-private-value")
    settings = evaluate_regressions.sandbox_settings()
    assert not settings.debug
    assert settings.openai_api_key is None
    assert settings.validation_timeout_seconds == 10


def test_final_live_inference_requires_reviewed_freeze(tmp_path, monkeypatch):
    from evaluation.who_when import audit_dataset, build_manifests

    items = [sample()]
    manifest = build_manifests(
        items,
        audit_dataset(items, ContextManager(ContextPolicy())),
        fixture_dir=tmp_path,
        development_count=0,
    )
    path = tmp_path / "manifest.json"
    write_once(path, manifest)
    monkeypatch.setattr(evaluate_attribution, "load_dataset", lambda _: items)
    provider = Mock()
    monkeypatch.setattr(evaluate_attribution, "OpenAILLMClient", provider)
    monkeypatch.setattr(
        "sys.argv",
        [
            "evaluate",
            "--live",
            "--partition",
            "final",
            "--manifest",
            str(path),
            "--output",
            str(tmp_path / "out"),
            "--max-calls",
            "1",
        ],
    )
    with pytest.raises(SystemExit):
        evaluate_attribution.main()
    provider.assert_not_called()


def test_freeze_rejects_incomplete_development(tmp_path, monkeypatch):
    root = tmp_path / "dev"
    Experiment(
        root, {"track": "attribution", "partition": "development", "samples": ["a"]}, resume=False
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "freeze",
            "--development",
            str(root),
            "--output",
            str(tmp_path / "freeze.json"),
            "--reviewed-development",
            "--review",
            str(tmp_path / "review.json"),
        ],
    )
    with pytest.raises(SystemExit):
        freeze_evaluation.main()
    assert not (tmp_path / "freeze.json").exists()


def test_comparison_requires_same_context_prompt_samples_and_complete_baseline(tmp_path):
    config = {
        "track": "attribution",
        "samples": ["a"],
        "manifest_digest": "x",
        "partition": "development",
        "inference": {
            "model": "luna",
            "reasoning_effort": "medium",
            "prompt_version": "v1",
            "context": 1,
        },
    }
    left = Experiment(tmp_path / "left", config, resume=False)
    other = deepcopy(config)
    other["inference"]["reasoning_effort"] = "high"
    right = Experiment(tmp_path / "right", other, resume=False)
    with pytest.raises(ValueError, match="complete"):
        compare(left, right)
    for experiment in (left, right):
        experiment.save(
            "a",
            {
                "scores": {
                    "agent_correct": True,
                    "step_correct": True,
                    "joint_correct": True,
                    "covered": True,
                    "abstained": False,
                },
                "prediction": {"confidence": "high"},
                "failure": None,
                "subset": "test",
                "context": None,
                "ground_truth": None,
            },
        )
    assert compare(left, right)["baseline"]["report"]["metrics"]["agent"]["accuracy"] == 1
    right.config["inference"]["context"] = 2
    with pytest.raises(ValueError, match="implementation differs"):
        compare(left, right)


def test_attribution_cli_call_budget_and_resume_with_fake_provider(tmp_path, monkeypatch):
    from evaluation.who_when import audit_dataset, build_manifests, samples_from_rows

    items = samples_from_rows(
        "Algorithm-Generated",
        [raw_record(question_ID=str(i), question=f"Task {i}") for i in range(3)],
    )
    manifest = build_manifests(
        items,
        audit_dataset(items, ContextManager(ContextPolicy())),
        fixture_dir=tmp_path,
        development_count=3,
    )
    path = tmp_path / "manifest.json"
    write_once(path, manifest)
    monkeypatch.setattr(evaluate_attribution, "load_dataset", lambda _: items)
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    client = Mock()
    client.attribute.return_value = result()
    monkeypatch.setattr(evaluate_attribution, "OpenAILLMClient", Mock(return_value=client))
    arguments = ["evaluate", "--manifest", str(path), "--output", str(tmp_path / "out"), "--live"]
    monkeypatch.setattr("sys.argv", arguments + ["--max-calls", "1"])
    evaluate_attribution.main()
    assert client.attribute.call_count == 1
    monkeypatch.setattr("sys.argv", arguments + ["--max-calls", "3", "--resume"])
    evaluate_attribution.main()
    assert client.attribute.call_count == 3
    experiment = Experiment(tmp_path / "out", read_json(tmp_path / "out/config.json"), resume=True)
    assert len(experiment.results()) == experiment.calls_started == 3
    assert sum(usage["input_tokens"] for usage in experiment.usages()) == 30
