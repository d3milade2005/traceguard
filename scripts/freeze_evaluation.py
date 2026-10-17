"""Freeze a fully completed and explicitly reviewed development configuration."""

import argparse
import subprocess
from pathlib import Path

from app.core.context import ContextPolicy
from evaluation.artifacts import Experiment, digest, read_json, timestamp, write_once
from evaluation.config import ROOT, ProviderSettings, inference_config
from evaluation.freeze import FREEZE_VERSION, dataset_identity, verify_saved_development
from evaluation.who_when import load_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reviewed-development", action="store_true", required=True)
    parser.add_argument(
        "--manifest", type=Path, default=ROOT / "research/manifests/who_when-v1.json"
    )
    parser.add_argument("--dataset", type=Path, default=ROOT / "data/who_when")
    parser.add_argument(
        "--review", type=Path, required=True, help="Offline development error-analysis artifact"
    )
    args = parser.parse_args()
    config = read_json(args.development / "config.json")
    if config["track"] != "attribution" or config["partition"] != "development":
        parser.error("Freeze must originate from a development attribution experiment")
    experiment = Experiment(args.development, config, resume=True)
    with experiment.locked():
        if not config["samples"] or any(
            experiment.completed(sid) is None for sid in config["samples"]
        ):
            parser.error("Every development sample must have a terminal result before freezing")
        manifest = read_json(args.manifest)
        samples = load_dataset(args.dataset)
        verified = verify_saved_development(experiment, manifest, samples)
        review = read_json(args.review)
        if (
            review.get("development_experiment_id") != experiment.id
            or review.get("development_results_digest") != digest(experiment.results())
            or review.get("attribution_prompt_decision")
            != f"keep_{config['inference']['prompt_version']}"
        ):
            parser.error("Review must identify this baseline and retain its attribution prompt")
        baseline = config["inference"]
        current = inference_config(
            baseline["model"],
            baseline["reasoning_effort"],
            ContextPolicy.model_validate(baseline["context_policy"]),
            ProviderSettings(),
        )
        # Code hardening may change the source digest. Every decision-affecting setting
        # must match; exact saved inputs/contexts/scoring were independently checked above.
        if any(
            current.get(k) != v
            for k, v in baseline.items()
            if k not in {"source_digest", "version", "runner_version"}
        ):
            parser.error(
                "Inference settings changed; a separately authorized development run is required"
            )
        final_members = [m for m in manifest["members"] if m["partition"] == "final"]
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        write_once(
            args.output,
            {
                "version": FREEZE_VERSION,
                "timestamp": timestamp(),
                "inference": current,
                "manifest_digest": config["manifest_digest"],
                "development_experiment_id": digest(config),
                "development_results_digest": digest(experiment.results()),
                "reviewed_development": True,
                "data": dataset_identity(args.dataset, samples),
                "final_manifest_digest": digest(final_members),
                "final_sample_count": len(final_members),
                "final_sample_ids": [m["sample_id"] for m in final_members],
                "offline_reverification": verified,
                "development_review_digest": digest(review),
                "code_revision": revision,
                "code_identity_note": (
                    "Working source digest is authoritative, including uncommitted changes."
                ),
                "final_provider_calls": 0,
            },
        )
    print("Frozen configuration saved:", args.output)


if __name__ == "__main__":
    main()
