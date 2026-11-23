"""Derive prospective-study counts from immutable scenario, provider and sandbox records."""

import argparse
import csv
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from examples.langgraph_refund.fresh_application import SCENARIOS


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def calculate(root):
    rows, calls = [], []
    for case, target, sequence in SCENARIOS:
        file = root / "cases" / case / "result.json"
        saved = read(file) if file.exists() else {}
        pipeline = saved.get("pipeline", {})
        attr = pipeline.get("attribution", {})
        spec = pipeline.get("regression_spec", {})
        test = pipeline.get("generated_test", {})
        validation = pipeline.get("validation", {})
        faulty, corrected = saved.get("faulty_oracle", {}), saved.get("corrected_oracle", {})
        a = bool(faulty.get("injection_observed")) and not faulty.get("additional_fault", True)
        b = (
            attr.get("status") == "completed"
            and attr.get("responsible_agent") == target
            and attr.get("decisive_sequence") == sequence
        )
        c = spec.get("status") == "completed" and spec.get("disposition") == "synthesizable"
        d = test.get("status") == "completed"
        e = (
            validation.get("verdict") == "valid"
            and validation.get("faulty_outcome") == "assertion_failed"
            and validation.get("corrected_outcome") == "passed"
        )
        f = bool(validation) and all(
            (
                pipeline.get("run_id") == attr.get("run_id"),
                attr.get("id") == spec.get("attribution_id"),
                spec.get("id") == test.get("regression_spec_id"),
                test.get("id") == validation.get("generated_test_id"),
            )
        )
        g = bool(corrected.get("passes"))
        repeats = pipeline.get("additional_pairs", [])
        row = {
            "id": case,
            "expected_agent": target,
            "expected_step": sequence,
            "started": bool(saved),
            "oracle_eligible": a,
            "predicted_agent": attr.get("responsible_agent"),
            "predicted_step": attr.get("decisive_sequence"),
            "attribution_status": attr.get("status", "not_started"),
            "attribution_decision": attr.get("decision"),
            "specification_status": spec.get("status", "not_started"),
            "specification_disposition": spec.get("disposition"),
            "compilation_status": test.get("status", "not_started"),
            "validation_verdict": validation.get("verdict", "not_started"),
            "analysis_status": pipeline.get("analysis", {}).get("status", "not_started"),
            "stop_reason": pipeline.get("analysis", {}).get("stop_reason"),
            "execution_error": saved.get("error"),
            "A": a,
            "B": b,
            "C": c,
            "D": d,
            "E": e,
            "F": f,
            "G": g,
            "full_chain_success": all((a, b, c, d, e, f, g)),
            "extra_pairs": len(repeats),
            "extra_valid_pairs": sum(
                p["faulty"]["outcome"] == "assertion_failed"
                and p["corrected"]["outcome"] == "passed"
                for p in repeats
            ),
            "source_digest": test.get("source_digest"),
            "provider_deviations_faulty": faulty.get("provider_deviations", []),
            "provider_deviations_corrected": corrected.get("provider_deviations", []),
        }
        if d:
            source = test["source_code"].encode()
            assert hashlib.sha256(source).hexdigest() == test["source_digest"]
            assert (root / "cases" / case / "test_generated.py").read_bytes() == source
            for log in (root / "cases" / case).glob("sandbox-*/command-*.json"):
                command = read(log)
                if command["args"][:3] == ["start", "--attach", "--interactive"]:
                    assert command["stdin_sha256"] == test["source_digest"]
        rows.append(row)
    for intent in sorted((root / "inference").glob("*/intent.json")):
        directory = intent.parent
        entry = read(intent)
        entry["response_saved"] = (directory / "response.json").exists()
        entry["error"] = (
            read(directory / "error.json") if (directory / "error.json").exists() else None
        )
        if (directory / "cost.json").exists():
            entry.update(read(directory / "cost.json"))
        calls.append(entry)
    metrics = {}

    def metric(name, numerator, denominator):
        metrics[name] = {
            "numerator": numerator,
            "denominator": denominator,
            "percent": round(100 * numerator / denominator, 2) if denominator else None,
        }

    for name in "ABCDEFG":
        metric("criterion_" + name, sum(r[name] for r in rows), 3)
    metric("full_chain", sum(r["full_chain_success"] for r in rows), 3)
    metric(
        "agent_agreement_planned", sum(r["predicted_agent"] == r["expected_agent"] for r in rows), 3
    )
    metric(
        "step_agreement_planned", sum(r["predicted_step"] == r["expected_step"] for r in rows), 3
    )
    metric(
        "valid_attribution_coverage",
        sum(r["attribution_decision"] == "attributed" for r in rows),
        3,
    )
    metric("attribution_abstention", sum(r["attribution_decision"] == "abstain" for r in rows), 3)
    metric("attribution_error", sum(r["attribution_status"] == "failed" for r in rows), 3)
    metric("review_required", sum(r["analysis_status"] == "review_required" for r in rows), 3)
    eligible = [r for r in rows if r["oracle_eligible"]]
    metric("joint_agreement_oracle_eligible", sum(r["B"] for r in eligible), len(eligible))
    specifications = sum(r["specification_status"] != "not_started" for r in rows)
    accepted = sum(r["C"] for r in rows)
    metric("specification_acceptance_submitted", accepted, specifications)
    metric("compilation_accepted_specs", sum(r["D"] for r in rows), accepted)
    validations = sum(r["validation_verdict"] != "not_started" for r in rows)
    metric("discrimination_validations", sum(r["E"] for r in rows), validations)
    completed_executions = sum(
        r["validation_verdict"] in {"valid", "not_reproduced", "not_discriminating"} for r in rows
    )
    metric("paired_execution_completion_compiled", completed_executions, sum(r["D"] for r in rows))
    metric(
        "inconclusive_validations",
        sum(r["validation_verdict"] == "inconclusive" for r in rows),
        validations,
    )
    metric(
        "deterministic_pairs_valid",
        sum(r["E"] + r["extra_valid_pairs"] for r in rows),
        validations + sum(r["extra_pairs"] for r in rows),
    )
    usage = {
        name: sum(c.get("usage", {}).get(name, 0) for c in calls)
        for name in ("input_tokens", "output_tokens", "total_tokens")
    }
    usage["cached_tokens"] = sum(
        c.get("usage", {}).get("input_tokens_details", {}).get("cached_tokens", 0) for c in calls
    )
    usage["cache_write_tokens"] = sum(
        c.get("usage", {}).get("input_tokens_details", {}).get("cache_write_tokens", 0)
        for c in calls
    )
    usage["reasoning_tokens_included_in_output"] = sum(
        c.get("usage", {}).get("output_tokens_details", {}).get("reasoning_tokens", 0)
        for c in calls
    )
    usage["estimated_cost_usd"] = str(sum(Decimal(c.get("estimated_cost_usd", "0")) for c in calls))
    usage["upper_rate_estimate_usd"] = str(
        sum(Decimal(c.get("upper_rate_estimate_usd", "0")) for c in calls)
    )
    usage["reserved_usd"] = str(sum(Decimal(c["reservation_usd"]) for c in calls))
    usage["generations_dispatched"] = len(calls)
    usage["token_count_operations"] = len(list((root / "inference/counts").glob("*.json")))
    usage["by_model"] = {
        m: sum(c["model"] == m for c in calls) for m in {c["model"] for c in calls}
    }
    usage["billing_verified"] = False
    usage["calls_without_reconciled_usage"] = sum("usage" not in c for c in calls)
    usage["estimated_cost_scope"] = "Recorded usage only; unresolved calls retain reservations"
    return {
        "protocol": "fresh-langgraph-service-end-to-end-v1",
        "planned": 3,
        "cases": rows,
        "metrics": metrics,
        "usage": usage,
        "calls": calls,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path(".local/langgraph-fresh-e2e-2026-11-23"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    result = calculate(args.input)
    (args.output / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    with (args.output / "cases.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(result["cases"][0]))
        writer.writeheader()
        writer.writerows(result["cases"])
    print(json.dumps({"metrics": result["metrics"], "usage": result["usage"]}, indent=2))


if __name__ == "__main__":
    main()
