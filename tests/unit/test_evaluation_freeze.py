from copy import deepcopy
from unittest.mock import Mock

import pytest

from app.core.context import ContextPolicy
from app.schemas.attributions import LLMResult
from app.services.context import ContextManager
from evaluation.artifacts import Experiment, digest
from evaluation.attribution import evaluate_sample
from evaluation.config import ProviderSettings, inference_config
from evaluation.freeze import (
    FREEZE_VERSION,
    dataset_identity,
    validate_freeze,
    verify_saved_development,
)
from evaluation.who_when import audit_dataset, build_manifests, samples_from_rows
from tests.unit.test_evaluation_data import prediction, raw_record


def setup_development(tmp_path):
    samples = samples_from_rows(
        "Algorithm-Generated",
        [raw_record(question_ID=str(i), question=f"Task {i}") for i in range(2)],
    )
    manifest = build_manifests(
        samples,
        audit_dataset(samples, ContextManager(ContextPolicy())),
        fixture_dir=tmp_path,
        development_count=1,
    )
    inference = inference_config(
        "fake-test", "medium", ContextPolicy(), ProviderSettings(_env_file=None)
    )
    config = {
        "track": "attribution",
        "partition": "development",
        "inference": inference,
        "manifest_digest": digest(manifest),
        "samples": manifest["development_sample"],
    }
    experiment = Experiment(tmp_path / "development", config, resume=False)
    client = Mock()
    client.attribute.return_value = LLMResult(attribution=prediction())
    for sample in samples:
        if sample.sample_id in config["samples"]:
            row = evaluate_sample(
                sample,
                ContextManager(ContextPolicy()),
                client,
                experiment,
                model="fake-test",
                effort="medium",
                max_calls=1,
            )
            experiment.save(sample.sample_id, row)
    return samples, manifest, experiment, client


def test_offline_equivalence_reuses_inputs_contexts_scores_without_provider(tmp_path):
    samples, manifest, experiment, client = setup_development(tmp_path)
    before = client.attribute.call_count
    report = verify_saved_development(experiment, manifest, samples)
    assert report["provider_calls"] == 0 and report["requests_contexts_scores_verified"] == 1
    assert client.attribute.call_count == before == 1


def test_offline_review_rejects_changed_scoring_input_or_partial_sample(tmp_path, monkeypatch):
    samples, manifest, experiment, _ = setup_development(tmp_path)
    row = experiment.completed(manifest["development_sample"][0])
    wrong = deepcopy(row)
    wrong["scores"]["agent_correct"] = not wrong["scores"]["agent_correct"]
    monkeypatch.setattr(experiment, "completed", lambda _: wrong)
    with pytest.raises(ValueError, match="scoring"):
        verify_saved_development(experiment, manifest, samples)
    wrong = deepcopy(row)
    wrong["input_digest"] = "0" * 64
    with pytest.raises(ValueError, match="request"):
        verify_saved_development(experiment, manifest, samples)
    monkeypatch.setattr(experiment, "completed", lambda _: None)
    with pytest.raises(ValueError, match="terminal"):
        verify_saved_development(experiment, manifest, samples)


def test_freeze_pins_data_full_final_manifest_and_every_inference_setting(tmp_path):
    samples, manifest, experiment, _ = setup_development(tmp_path)
    identity = dataset_identity(tmp_path, samples)
    final = [m for m in manifest["members"] if m["partition"] == "final"]
    ids = [m["sample_id"] for m in final]
    config = experiment.config | {"partition": "final", "samples": ids}
    freeze = {
        "version": FREEZE_VERSION,
        "reviewed_development": True,
        "inference": config["inference"],
        "manifest_digest": digest(manifest),
        "data": identity,
        "final_manifest_digest": digest(final),
        "final_sample_count": len(ids),
        "final_sample_ids": ids,
    }
    validate_freeze(freeze, config, manifest, identity)
    for field in (
        "model",
        "reasoning_effort",
        "prompt_version",
        "context_policy_version",
        "scoring_version",
        "semantics_version",
        "max_output_tokens",
        "timeout_seconds",
        "source_digest",
        "runner_version",
    ):
        changed = deepcopy(config)
        changed["inference"][field] = "changed"
        with pytest.raises(ValueError, match="Frozen"):
            validate_freeze(freeze, changed, manifest, identity)
    with pytest.raises(ValueError, match="Frozen"):
        validate_freeze(freeze, config | {"samples": []}, manifest, identity)
    with pytest.raises(ValueError, match="Frozen"):
        validate_freeze(freeze, config, manifest, identity | {"dataset_digest": "changed"})
