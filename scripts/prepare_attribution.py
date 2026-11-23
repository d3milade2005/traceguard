"""Prepare immutable Experiment A evidence and budgets offline; never invokes a model."""

import argparse
import hashlib
from collections import Counter
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from app.core.context import ContextPolicy
from app.services.attribution_input import build_attribution_request, input_digest
from app.services.context import ContextManager
from evaluation.artifacts import digest, read_json, write_once
from evaluation.config import ROOT, ProviderSettings, inference_config
from evaluation.context import analysis_context
from evaluation.freeze import dataset_identity
from evaluation.preparation import (
    PRICING,
    cost_bound,
    environment_identity,
    request_body,
    snapshot_repository,
)
from evaluation.who_when import labels, load_dataset, validate_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=ROOT / "data/who_when")
    parser.add_argument(
        "--manifest", type=Path, default=ROOT / "research/manifests/who_when-v1.json"
    )
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Preparation exists; use a new versioned directory")
    samples = load_dataset(args.dataset)
    manifest = read_json(args.manifest)
    validate_manifest(manifest, samples)
    members = manifest["members"]
    counts = Counter(m["partition"] for m in members)
    if counts != {"development": 35, "final": 149}:
        parser.error("The preserved 35-development/149-final partition is required")
    provider = ProviderSettings(
        _env_file=None, openai_timeout_seconds=60, openai_max_output_tokens=4096
    )
    policy = ContextPolicy()
    inference = inference_config("gpt-5.6-terra", "high", policy, provider)
    args.output.mkdir(parents=True)
    source = snapshot_repository(ROOT, args.output)
    write_once(args.output / "environment.json", environment_identity())
    write_once(args.output / "manifest.json", manifest)
    write_once(args.output / "pricing.json", PRICING)
    data = dataset_identity(args.dataset, samples)
    write_once(args.output / "dataset-identity.json", data)
    by_id = {s.sample_id: s for s in samples}
    annotations = []
    for member in members:
        sample = by_id[member["sample_id"]]
        annotations.append(member | {"labels": labels(sample, sample.canonical())})
    write_once(args.output / "annotation-audit.json", {"records": annotations})
    manager = ContextManager(policy)
    configs = {}
    budgets = {}
    for partition in ("development", "final"):
        ids = (
            manifest["development_sample"]
            if partition == "development"
            else [m["sample_id"] for m in members if m["partition"] == "final"]
        )
        config = {
            "track": "attribution",
            "inference": inference,
            "manifest_digest": digest(manifest),
            "partition": partition,
            "samples": ids,
        }
        configs[partition] = config
        write_once(args.output / f"{partition}-candidate.json", config)
        requests = []
        for sid in ids:
            trace = by_id[sid].canonical()
            context = analysis_context(manager, trace)
            request = build_attribution_request(
                context, trace, model="gpt-5.6-terra", reasoning_effort="high"
            )
            body = request_body(request, max_output_tokens=4096)
            filename = f"requests/{digest(sid)}.json"
            record = {
                "sample_id": sid,
                "request": request.model_dump(mode="json"),
                "responses_body": body,
                "context": context.model_dump(mode="json"),
            }
            path = args.output / partition / filename
            write_once(path, record)
            requests.append(
                {
                    "sample_id": sid,
                    "input_sha256": input_digest(request),
                    "request_sha256": digest(request.model_dump(mode="json")),
                    "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "artifact": filename,
                    **cost_bound(body, timeout_seconds=60),
                }
            )
        total = sum((Decimal(r["planning_upper_cost_usd"]) for r in requests), Decimal(0))
        budget = {
            "provider_calls_made": 0,
            "requests": requests,
            "maximum_requests": len(ids),
            "sdk_retries": 0,
            "sample_retries": 0,
            "pricing_digest": digest(PRICING),
            "candidate_configuration_digest": digest(config),
            "planning_upper_cost_usd": str(total),
            "rounded_planning_budget_usd": str(total.quantize(Decimal("0.01"), ROUND_CEILING)),
            "authorization": "Not approved; live execution requires explicit user budget approval",
        }
        budgets[partition] = {k: v for k, v in budget.items() if k != "requests"}
        write_once(args.output / partition / "budget.json", budget)
    final_labels = [a["labels"] for a in annotations if a["partition"] == "final"]
    eligibility = {
        "agent": sum(a["eligible_agent_scoring"] for a in final_labels),
        "step": sum(a["eligible_step_scoring"] for a in final_labels),
        "joint": sum(
            a["eligible_agent_scoring"] and a["eligible_step_scoring"] for a in final_labels
        ),
    }
    if eligibility != {"agent": 147, "step": 146, "joint": 144}:
        raise ValueError("Annotation eligibility differs from the audited dataset")
    protocol = {
        "version": "experiment-a-prospective-v1",
        "status": "offline_prepared_awaiting_paid_development_approval",
        "source_snapshot_digest": source["working_tree_sha256"],
        "base_commit": source["base_commit"],
        "data": data,
        "manifest_digest": digest(manifest),
        "inference": inference,
        "partition_counts": dict(counts),
        "final_scoring_eligibility": eligibility,
        "anomaly_policy": "Preserve raw labels; independent exact eligibility; no repairs",
        "holdout_scope": "Reserved from recorded inference, not blind to structural/label audit",
        "retry_policy": "One attempt per sample; terminal failures and abstentions are retained",
        "resume_policy": (
            "Reuse parsed results or recover a captured visible response offline. "
            "An intent without response/error evidence stops for reconciliation; never paid retry."
        ),
        "final_freeze": "Not issued: requires actual Terra development execution and review",
        "budgets": budgets,
    }
    write_once(args.output / "protocol.json", protocol)
    print({"preparation": str(args.output), "eligibility": eligibility, "budgets": budgets})


if __name__ == "__main__":
    main()
