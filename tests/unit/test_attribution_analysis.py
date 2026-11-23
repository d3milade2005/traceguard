from copy import deepcopy
from decimal import Decimal
from unittest.mock import Mock

import pytest

from app.core.context import ContextPolicy
from app.core.exceptions import LLMConfigurationError
from app.schemas.attributions import LLMResult
from app.services.context import ContextManager
from evaluation.artifacts import Experiment, digest, read_json, write_once
from evaluation.attribution import evaluate_sample
from evaluation.attribution_analysis import (
    cluster_interval,
    error_categories,
    recorded_usage,
    verify_records,
    wilson_interval,
)
from evaluation.who_when import audit_dataset, build_manifests, samples_from_rows
from scripts import evaluate_attribution
from scripts import freeze_evaluation as freeze_cli
from tests.unit.test_attribution_evidence import recording_client, terra_request
from tests.unit.test_evaluation_data import prediction, raw_record, sample
from tests.unit.test_evaluation_freeze import setup_development
from tests.unit.test_openai_attribution import response_body


def test_uncertainty_handles_empty_and_boundary_counts():
    assert wilson_interval(0, 0) is None
    assert wilson_interval(0, 10)[0] == 0
    assert wilson_interval(10, 10)[1] == 1
    interval = wilson_interval(50, 100)
    assert interval[0] == pytest.approx(0.40383, abs=0.00001)
    assert interval[1] == pytest.approx(0.59617, abs=0.00001)


def test_group_resampling_is_repeatable_and_preserves_related_cases():
    rows = [
        {"sample_id": str(i), "scores": {"joint_correct": value}}
        for i, value in enumerate([True, True, False, None])
    ]
    groups = {"0": "same-question", "1": "same-question", "2": "other", "3": "other"}
    first = cluster_interval(rows, groups, "joint_correct", repetitions=100)
    assert first == cluster_interval(rows, groups, "joint_correct", repetitions=100)
    assert first == [0, 1]
    assert cluster_interval(rows[:2], groups, "joint_correct") is None


def test_error_categories_separate_abstention_failure_and_partial_eligibility():
    base = {
        "sample_id": "a",
        "subset": "fixture",
        "ground_truth": {},
        "context": None,
        "scores": {
            "agent_correct": True,
            "step_correct": False,
            "joint_correct": False,
            "abstained": False,
        },
    }
    rows = [base, deepcopy(base), deepcopy(base), deepcopy(base)]
    rows[1]["failure"] = {"code": "FIXTURE"}
    rows[2]["scores"]["abstained"] = True
    rows[3]["scores"]["joint_correct"] = None
    result = error_categories(rows)
    assert result["counts"] == {
        "correct_agent_wrong_step": 1,
        "attempt_error": 1,
        "abstention": 1,
        "not_joint_scoring_eligible": 1,
    }


def test_cost_uses_recorded_cache_categories_and_does_not_add_reasoning(monkeypatch, tmp_path):
    body = response_body()
    body["model"], body["service_tier"] = "gpt-5.6-terra", "default"
    body["usage"]["input_tokens_details"] = {"cached_tokens": 2, "cache_write_tokens": 1}
    client, experiment, _, _ = recording_client(monkeypatch, tmp_path, body)
    experiment.call(
        "fixture",
        "attribution",
        terra_request(),
        LLMResult,
        lambda: client.attribute(terra_request()),
        max_calls=1,
    )
    result = recorded_usage(experiment.root)
    assert result["input_tokens"] == 10 and result["output_tokens"] == 30
    assert Decimal(result["known_usage_estimate_lower_usd"]) == Decimal("0.0003769")
    assert result["known_usage_estimate_lower_usd"] == result["known_usage_estimate_upper_usd"]
    assert result["billing_verified"] is False


def test_verified_analysis_rejects_modified_scores(tmp_path):
    items = [sample()]
    manager = ContextManager(ContextPolicy())
    manifest = build_manifests(
        items, audit_dataset(items, manager), fixture_dir=tmp_path, development_count=1
    )
    config = {
        "partition": "development",
        "samples": manifest["development_sample"],
        "manifest_digest": digest(manifest),
        "inference": {
            "context_policy": ContextPolicy().model_dump(),
            "model": "offline-fixture",
            "reasoning_effort": "medium",
        },
    }
    experiment = Experiment(tmp_path / "offline-fixture", config, resume=False)
    client = Mock()
    client.attribute.return_value = LLMResult(attribution=prediction())
    row = evaluate_sample(
        items[0], manager, client, experiment, model="offline-fixture", effort="medium", max_calls=1
    )
    experiment.save(items[0].sample_id, row)
    _, verified = verify_records(experiment.root, manifest, items)
    assert verified[0]["scores"]["joint_correct"] is True
    path = experiment.result_path(items[0].sample_id)
    saved = read_json(path)
    saved["scores"]["joint_correct"] = False
    import json

    path.write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(ValueError, match="scores differ"):
        verify_records(experiment.root, manifest, items)


def test_cli_stops_on_configuration_failure_without_attempting_remaining_cases(
    tmp_path, monkeypatch
):
    items = samples_from_rows(
        "Algorithm-Generated",
        [raw_record(question_ID=str(i), question=f"Offline fixture {i}") for i in range(2)],
    )
    manifest = build_manifests(
        items,
        audit_dataset(items, ContextManager(ContextPolicy())),
        fixture_dir=tmp_path,
        development_count=2,
    )
    path = tmp_path / "manifest.json"
    write_once(path, manifest)
    monkeypatch.setattr(evaluate_attribution, "load_dataset", lambda _: items)
    client = Mock()
    client.attribute.side_effect = LLMConfigurationError
    monkeypatch.setattr(evaluate_attribution, "OpenAILLMClient", lambda *args, **kwargs: client)
    monkeypatch.setenv("OPENAI_API_KEY", "offline-fake-credential")
    monkeypatch.setattr(
        "sys.argv",
        [
            "evaluate",
            "--manifest",
            str(path),
            "--output",
            str(tmp_path / "offline-fixture"),
            "--model",
            "gpt-5.6-terra",
            "--reasoning-effort",
            "high",
            "--max-calls",
            "2",
            "--live",
        ],
    )
    evaluate_attribution.main()
    assert client.attribute.call_count == 1
    report = read_json(next((tmp_path / "offline-fixture").glob("report-*.json")))
    assert report["completed_samples"] == report["remaining_samples"] == 1
    assert report["metrics"]["failures"] == 1


def test_final_freeze_rejects_new_inference_fields_not_used_in_development(tmp_path, monkeypatch):
    items, manifest, experiment, _ = setup_development(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    review_path = tmp_path / "review.json"
    output = tmp_path / "freeze.json"
    write_once(manifest_path, manifest)
    write_once(
        review_path,
        {
            "development_experiment_id": experiment.id,
            "development_results_digest": digest(experiment.results()),
            "attribution_prompt_decision": "keep_attribution-v1",
        },
    )
    monkeypatch.setattr(freeze_cli, "load_dataset", lambda _: items)
    monkeypatch.setattr(
        freeze_cli,
        "inference_config",
        lambda *args, **kwargs: experiment.config["inference"] | {"unreviewed_parameter": "new"},
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "freeze",
            "--development",
            str(experiment.root),
            "--manifest",
            str(manifest_path),
            "--review",
            str(review_path),
            "--output",
            str(output),
            "--reviewed-development",
        ],
    )
    with pytest.raises(SystemExit):
        freeze_cli.main()
    assert not output.exists()
