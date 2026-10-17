"""Explicit, offline-by-default Who&When audit and attribution experiments."""

import argparse
import json
from collections import Counter
from pathlib import Path

from app.core.context import ContextPolicy
from app.integrations.llm.openai import OpenAILLMClient
from app.services.context import ContextManager
from evaluation.artifacts import (
    CallLimit,
    Experiment,
    UncertainCall,
    digest,
    ensure_snapshot,
    read_json,
    write_report,
)
from evaluation.attribution import evaluate_sample
from evaluation.config import ROOT, ProviderSettings, inference_config
from evaluation.context import RUN_ID_POLICY
from evaluation.freeze import dataset_identity, validate_freeze
from evaluation.metrics import build_report, context_summary, eligibility_summary
from evaluation.who_when import (
    SCORING_VERSION,
    SEMANTICS_VERSION,
    audit_dataset,
    build_manifests,
    load_dataset,
    validate_manifest,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "data/who_when")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--context-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--partition", choices=("development", "final"), default="development")
    parser.add_argument("--max-samples", type=int, default=16)
    parser.add_argument("--max-calls", type=int, default=0)
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument(
        "--reasoning-effort", choices=("low", "medium", "high", "xhigh"), default="medium"
    )
    parser.add_argument("--context-policy", type=Path)
    parser.add_argument(
        "--freeze", type=Path, help="Reviewed development freeze required for final inference"
    )
    args = parser.parse_args()
    if args.max_samples < 1 or args.max_calls < 0:
        parser.error("Sample limit must be positive; call limit must be nonnegative")
    if args.live and (args.context_only or args.dry_run):
        parser.error("--live cannot be combined with offline modes")
    samples = load_dataset(args.dataset)
    policy = (
        ContextPolicy.model_validate(read_json(args.context_policy))
        if args.context_policy
        else ContextPolicy()
    )
    manager = ContextManager(policy)
    if args.context_only:
        experiment = Experiment(
            args.output,
            {
                "track": "context-audit",
                "version": "phase8-v1",
                "run_id_policy": RUN_ID_POLICY,
                "semantics_version": SEMANTICS_VERSION,
                "scoring_version": SCORING_VERSION,
                "context_source_digest": digest(
                    (ROOT / "app/services/context.py").read_text(encoding="utf-8")
                ),
                "adapter_source_digest": digest(
                    (ROOT / "app/integrations/traces/who_when.py").read_text(encoding="utf-8")
                ),
                "context_policy": policy.model_dump(mode="json"),
                "dataset_digest": digest(sorted((s.sample_id, digest(s.record)) for s in samples)),
            },
            resume=args.resume,
        )
        with experiment.locked():
            audits = audit_dataset(samples, manager)
            for row in audits:
                if experiment.completed(row["sample_id"]) is None:
                    experiment.save(row["sample_id"], row)
            manifest = build_manifests(
                samples, audits, fixture_dir=ROOT / "tests/fixtures/who_when"
            )
            ensure_snapshot(args.output / "manifest.json", manifest)
            report = {
                "track": "context-audit",
                "metrics": context_summary(audits),
                "eligibility": eligibility_summary(audits),
                "by_subset": {
                    subset: context_summary([a for a in audits if a["subset"] == subset])
                    for subset in sorted({a["subset"] for a in audits})
                },
                "anomalies": {},
                "development_sample_count": len(manifest["development_sample"]),
                "partition_counts": {
                    p: sum(m["partition"] == p for m in manifest["members"])
                    for p in ("development", "final")
                },
            }
            report["anomalies"] = dict(
                sorted(
                    Counter(
                        a
                        for row in audits
                        if row["ground_truth"]
                        for a in row["ground_truth"]["annotation_anomaly"]
                    ).items()
                )
            )
            report["experiment_id"] = experiment.id
            report["configuration"] = experiment.config
            write_report(args.output, report)
            print(json.dumps(report, indent=2))
        return
    if args.manifest is None:
        parser.error("--manifest is required; create the offline context audit first")
    manifest = read_json(args.manifest)
    validate_manifest(manifest, samples)
    selected_ids = (
        manifest["development_sample"]
        if args.partition == "development"
        else [m["sample_id"] for m in manifest["members"] if m["partition"] == "final"]
    )
    selected_ids = selected_ids[: args.max_samples]
    if not selected_ids:
        parser.error("The selected manifest partition has no samples")
    by_id = {s.sample_id: s for s in samples}
    provider = ProviderSettings()
    config = {
        "track": "attribution",
        "inference": inference_config(args.model, args.reasoning_effort, policy, provider),
        "manifest_digest": digest(manifest),
        "partition": args.partition,
        "samples": selected_ids,
    }
    if args.partition == "final" and (args.live or args.freeze is not None):
        if args.freeze is None:
            parser.error("Final inference requires --freeze from reviewed development")
        freeze = read_json(args.freeze)
        try:
            validate_freeze(freeze, config, manifest, dataset_identity(args.dataset, samples))
        except ValueError as exc:
            parser.error(str(exc))
        config["freeze_digest"] = digest(freeze)
    if not args.live:
        # Dry runs never create sample predictions or mark them as completed.
        experiment = Experiment(args.output, config, resume=args.resume)
        complete = sum(experiment.completed(sid) is not None for sid in selected_ids)
        plan = {
            "mode": "dry-run",
            "planned_calls": len(selected_ids),
            "sample_count": len(selected_ids),
            "already_completed_samples": complete,
            "remaining_samples": len(selected_ids) - complete,
            "calls_already_started": experiment.calls_started,
            "max_calls_total": args.max_calls,
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
            "prompt_version": config["inference"]["prompt_version"],
            "context_policy_version": config["inference"]["context_policy_version"],
            "scoring_version": config["inference"]["scoring_version"],
            "semantics_version": config["inference"]["semantics_version"],
            "manifest_digest": config["manifest_digest"],
            "sample_ids": selected_ids,
        }
        write_report(experiment.root, plan)
        print(json.dumps(plan, indent=2))
        return
    if args.max_calls < 1:
        parser.error("Paid inference requires an explicit positive --max-calls limit")
    if provider.openai_api_key is None:
        parser.error("OPENAI_API_KEY is not configured")
    experiment = Experiment(args.output, config, resume=args.resume)
    with experiment.locked():
        complete = sum(experiment.completed(sid) is not None for sid in selected_ids)
        print(
            {
                "planned_calls_upper_bound": len(selected_ids),
                "already_completed_samples": complete,
                "remaining_samples": len(selected_ids) - complete,
                "calls_already_started": experiment.calls_started,
                "max_calls_total": args.max_calls,
                "model": args.model,
                "reasoning_effort": args.reasoning_effort,
                "prompt_version": config["inference"]["prompt_version"],
                "context_policy_version": config["inference"]["context_policy_version"],
                "scoring_version": config["inference"]["scoring_version"],
                "semantics_version": config["inference"]["semantics_version"],
                "manifest_digest": config["manifest_digest"],
            },
            flush=True,
        )
        client = OpenAILLMClient(
            provider.openai_api_key,
            timeout_seconds=provider.openai_timeout_seconds,
            max_output_tokens=provider.openai_max_output_tokens,
        )
        for sid in selected_ids:
            if experiment.completed(sid) is not None:
                continue
            try:
                row = evaluate_sample(
                    by_id[sid],
                    manager,
                    client,
                    experiment,
                    model=args.model,
                    effort=args.reasoning_effort,
                    max_calls=args.max_calls,
                )
            except (CallLimit, UncertainCall) as exc:
                print(str(exc), flush=True)
                break
            experiment.save(sid, row)
        rows = experiment.results()
        usages = experiment.usages()
        report = build_report(rows, "attribution", planned=len(selected_ids), usages=usages)
        report["calls_started"] = experiment.calls_started
        report["experiment_id"] = experiment.id
        report["configuration"] = experiment.config
        write_report(args.output, report)
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
