"""Offline equivalence verification and immutable final configuration validation."""

import hashlib
import json
from pathlib import Path

from app.core.context import ContextPolicy
from app.schemas.attributions import AttributionDecision
from app.services.attribution_input import build_attribution_request, input_digest
from app.services.attribution_validation import validate_attribution
from app.services.context import ContextManager
from evaluation.artifacts import Experiment, digest, read_json
from evaluation.context import analysis_context, context_statistics
from evaluation.metrics import score
from evaluation.who_when import Sample, labels, validate_manifest

FREEZE_VERSION = "phase8-freeze-v2"
RUNNER_VERSION = "phase8-runner-v2"


def dataset_identity(root: Path, samples: list[Sample]) -> dict:
    return {
        "dataset_digest": digest(sorted((s.sample_id, digest(s.record)) for s in samples)),
        "source_files": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.glob("*.parquet"))
        },
    }


def verify_saved_development(experiment: Experiment, manifest: dict, samples: list[Sample]) -> dict:
    """Reconstruct inputs and scores; no provider client is imported or called."""
    config = experiment.config
    validate_manifest(manifest, samples)
    if config.get("manifest_digest") != digest(manifest):
        raise ValueError("Development manifest changed")
    if config.get("samples") != manifest["development_sample"]:
        raise ValueError("Development must contain the complete fixed sample")
    by_id = {s.sample_id: s for s in samples}
    manager = ContextManager(ContextPolicy.model_validate(config["inference"]["context_policy"]))
    checks = []
    for sid in config["samples"]:
        row = experiment.completed(sid)
        if row is None:
            raise ValueError("Every development sample must have a terminal result")
        sample = by_id[sid]
        trace = sample.canonical()
        context = analysis_context(manager, trace)
        request = build_attribution_request(
            context,
            trace,
            model=config["inference"]["model"],
            reasoning_effort=config["inference"]["reasoning_effort"],
        )
        intent = read_json(
            experiment.root / "calls" / f"{digest([sid, 'attribution'])}.intent.json"
        )
        if input_digest(request) != row["input_digest"] or intent["input_digest"] != digest(
            request.model_dump(mode="json")
        ):
            raise ValueError("Development attribution request changed")
        if context_statistics(context) != row["context"]:
            raise ValueError("Development context changed")
        truth = labels(sample, trace)
        if truth != row["ground_truth"]:
            raise ValueError("Development labels or eligibility changed")
        prediction = (
            AttributionDecision.model_validate_json(json.dumps(row["prediction"]))
            if row["prediction"]
            else None
        )
        if prediction is not None and row["failure"] is None:
            validate_attribution(prediction, context, trace)
        if score(prediction, truth, valid=row["failure"] is None) != row["scores"]:
            raise ValueError("Development scoring changed")
        checks.append(
            {
                "sample_id": sid,
                "input_digest": row["input_digest"],
                "context_digest": digest(context.model_dump(mode="json")),
                "scores_digest": digest(row["scores"]),
            }
        )
    return {
        "version": "offline-development-equivalence-v1",
        "provider_calls": 0,
        "requests_contexts_scores_verified": len(checks),
        "samples": checks,
        "note": (
            "Saved decisions/failures are reused; rejected historical output "
            "cannot be reconstructed."
        ),
    }


def validate_freeze(freeze: dict, config: dict, manifest: dict, identity: dict) -> None:
    final_ids = [m["sample_id"] for m in manifest["members"] if m["partition"] == "final"]
    final_members = [m for m in manifest["members"] if m["partition"] == "final"]
    if (
        freeze.get("version") != FREEZE_VERSION
        or freeze.get("reviewed_development") is not True
        or freeze.get("inference") != config["inference"]
        or freeze.get("manifest_digest") != digest(manifest)
        or freeze.get("data") != identity
        or freeze.get("final_manifest_digest") != digest(final_members)
        or freeze.get("final_sample_ids") != final_ids
        or freeze.get("final_sample_count") != len(final_ids)
        or config["samples"] != final_ids
    ):
        raise ValueError(
            "Frozen inference, dataset, scoring, code, or complete final manifest differs"
        )
