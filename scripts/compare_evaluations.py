"""Compare identical experimental protocols without inference or automatic model selection."""

import argparse
from pathlib import Path

from evaluation.artifacts import Experiment, read_json, write_once
from evaluation.metrics import build_report


def compare(baseline: Experiment, candidate: Experiment) -> dict:
    left, right = baseline.config, candidate.config
    if left["track"] != right["track"] or left["samples"] != right["samples"]:
        raise ValueError("Comparisons require the same track and ordered sample manifest")
    for key in ("manifest_digest", "partition", "base_image", "suite_image"):
        if left.get(key) != right.get(key):
            raise ValueError("Comparison protocol differs")
    left_inference, right_inference = left["inference"].copy(), right["inference"].copy()
    for key in ("model", "reasoning_effort"):
        left_inference.pop(key)
        right_inference.pop(key)
    if left_inference != right_inference:
        raise ValueError("Prompt, context, scoring, or implementation differs")
    if any(
        experiment.completed(sid) is None
        for experiment in (baseline, candidate)
        for sid in left["samples"]
    ):
        raise ValueError("Both experiments must be complete; inspect the baseline first")
    if left["track"] == "regressions" and (
        read_json(baseline.root / "runtime.json") != read_json(candidate.root / "runtime.json")
    ):
        raise ValueError("Sandbox runtimes differ")
    return {
        name: {
            "experiment_id": experiment.id,
            "inference": experiment.config["inference"],
            "report": build_report(
                experiment.results(),
                left["track"],
                planned=len(left["samples"]),
                usages=experiment.usages(),
            ),
        }
        for name, experiment in (("baseline", baseline), ("candidate", candidate))
    } | {"interpretation": "Descriptive comparison with identical samples and protocol."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline = Experiment(args.baseline, read_json(args.baseline / "config.json"), resume=True)
    candidate = Experiment(args.candidate, read_json(args.candidate / "config.json"), resume=True)
    write_once(args.output, compare(baseline, candidate))


if __name__ == "__main__":
    main()
