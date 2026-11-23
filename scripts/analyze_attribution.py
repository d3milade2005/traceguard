"""Generate verified attribution results from terminal records, with no inference."""

import argparse
from pathlib import Path

from evaluation.artifacts import read_json, write_once
from evaluation.attribution_analysis import (
    error_categories,
    recorded_usage,
    uncertainty,
    verify_records,
)
from evaluation.config import ROOT
from evaluation.metrics import attribution_summary
from evaluation.who_when import load_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--manifest", type=Path, default=ROOT / "research/manifests/who_when-v1.json"
    )
    parser.add_argument("--dataset", type=Path, default=ROOT / "data/who_when")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Analysis exists; choose a new directory")
    manifest = read_json(args.manifest)
    config, rows = verify_records(args.experiment, manifest, load_dataset(args.dataset))
    groups = {m["sample_id"]: m["group_id"] for m in manifest["members"]}
    summary = attribution_summary(rows)
    summary["error_rate"] = summary["failures"] / len(rows) if rows else None
    report = {
        "partition": config["partition"],
        "configuration": config,
        "metrics": summary,
        "uncertainty": uncertainty(rows, groups),
        "by_subset": {
            subset: {
                "metrics": attribution_summary([r for r in rows if r["subset"] == subset]),
                "uncertainty": uncertainty([r for r in rows if r["subset"] == subset], groups),
            }
            for subset in sorted({r["subset"] for r in rows})
        },
        "usage": recorded_usage(args.experiment),
    }
    write_once(args.output / "metrics.json", report)
    write_once(args.output / "error-analysis.json", error_categories(rows))
    # A machine-readable table avoids hand-transcribed numerators and percentages.
    table = []
    for label, values in [("Overall", summary)] + [
        (name, group["metrics"]) for name, group in report["by_subset"].items()
    ]:
        for metric in ("agent", "step", "joint"):
            v = values[metric]
            table.append({"group": label, "metric": metric, **v})
    write_once(args.output / "results-table.json", {"rows": table})
    print({"output": str(args.output), "partition": config["partition"], "metrics": summary})


if __name__ == "__main__":
    main()
