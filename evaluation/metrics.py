"""Pure exact scoring and deterministic summaries with explicit denominators."""

from collections import Counter
from statistics import mean, median

from app.schemas.attributions import AttributionDecision


def fraction(numerator: int | float, denominator: int | float) -> float | None:
    return numerator / denominator if denominator else None


def score(prediction: AttributionDecision | None, truth: dict, *, valid: bool = True) -> dict:
    covered = valid and prediction is not None and prediction.decision == "attributed"
    agent = (
        covered and prediction.responsible_agent == truth["scoring_agent"]
        if truth["eligible_agent_scoring"]
        else None
    )
    step = (
        covered and prediction.decisive_sequence == truth["scoring_sequence"]
        if truth["eligible_step_scoring"]
        else None
    )
    return {
        "agent_correct": agent,
        "step_correct": step,
        "joint_correct": bool(agent and step) if agent is not None and step is not None else None,
        "covered": covered,
        "abstained": valid and prediction is not None and prediction.decision == "abstain",
    }


def accuracy(rows: list[dict], key: str) -> dict:
    eligible = [r["scores"][key] for r in rows if r["scores"][key] is not None]
    correct = sum(value is True for value in eligible)
    return {
        "correct": correct,
        "eligible": len(eligible),
        "accuracy": fraction(correct, len(eligible)),
    }


def attribution_summary(rows: list[dict]) -> dict:
    total = len(rows)
    covered = [r for r in rows if r["scores"]["covered"]]
    abstentions = sum(r["scores"]["abstained"] for r in rows)
    invalid = sum(r.get("invalid_semantic_output", False) for r in rows)
    confidences = Counter(
        r["prediction"]["confidence"]
        for r in rows
        if r.get("prediction") and r.get("failure") is None
    )
    return {
        "total": total,
        "agent": accuracy(rows, "agent_correct"),
        "step": accuracy(rows, "step_correct"),
        "joint": accuracy(rows, "joint_correct"),
        "covered": len(covered),
        "coverage": fraction(len(covered), total),
        "abstentions": abstentions,
        "abstention_rate": fraction(abstentions, total),
        "failures": sum(r.get("failure") is not None for r in rows),
        "invalid_semantic_outputs": invalid,
        "invalid_semantic_output_rate": fraction(invalid, total),
        "covered_agent": accuracy(covered, "agent_correct"),
        "covered_step": accuracy(covered, "step_correct"),
        "covered_joint": accuracy(covered, "joint_correct"),
        "confidence_distribution": {
            level: confidences[level] for level in ("low", "medium", "high")
        },
        "accuracy_by_confidence": {
            level: {
                "all": accuracy(
                    [
                        r
                        for r in rows
                        if r.get("prediction")
                        and r["prediction"]["confidence"] == level
                        and r.get("failure") is None
                    ],
                    "agent_correct",
                ),
                "covered": accuracy(
                    [r for r in covered if r["prediction"]["confidence"] == level], "agent_correct"
                ),
            }
            for level in ("low", "medium", "high")
        },
    }


def context_summary(rows: list[dict]) -> dict:
    contexts = [r["context"] for r in rows if r.get("context") is not None]
    original = [c["estimated_original_size"] for c in contexts]
    selected = [c["estimated_selected_size"] for c in contexts]
    retained = Counter(
        r.get("decisive_step_retention")
        for r in rows
        if r.get("ground_truth") and r["ground_truth"]["eligible_step_scoring"]
    )
    eligible = sum(retained.values())
    full = sum(c["full_trace_used"] for c in contexts)
    return {
        "total": len(rows),
        "processed": len(contexts),
        "failed": len(rows) - len(contexts),
        "full": full,
        "pruned": len(contexts) - full,
        "full_fraction": fraction(full, len(rows)),
        "pruned_fraction": fraction(len(contexts) - full, len(rows)),
        "size_unit": "utf8_bytes",
        "mean_original_size": mean(original) if original else None,
        "mean_selected_size": mean(selected) if selected else None,
        "selected_over_original_ratio": fraction(sum(selected), sum(original)),
        "step_eligible": eligible,
        "decisive_complete": retained["complete"],
        "decisive_truncated": retained["truncated"],
        "decisive_omitted": retained["omitted"],
        "decisive_context_unavailable": retained[None],
        "decisive_step_retention_rate": fraction(retained["complete"], eligible),
        "decisive_sequence_visible_rate": fraction(
            retained["complete"] + retained["truncated"], eligible
        ),
    }


def usage_summary(usages: list[dict]) -> dict:
    result: dict = {
        "responses_with_usage": len(usages),
        "provider_cost": None,
        "cost_note": "Responses supply token counts, not billed dollar cost; no price assumed.",
    }
    for field in ("input_tokens", "output_tokens", "reasoning_tokens"):
        values = [u[field] for u in usages if u.get(field) is not None]
        result[field] = {"total": sum(values) if values else None, "known_responses": len(values)}
    latency = sorted(u["latency_ms"] for u in usages if u.get("latency_ms") is not None)
    result["provider_latency_ms"] = {
        "mean": mean(latency) if latency else None,
        "median": median(latency) if latency else None,
        "known_responses": len(latency),
    }
    return result


def eligibility_summary(rows: list[dict]) -> dict:
    truths = [row["ground_truth"] for row in rows if row.get("ground_truth")]
    agent = sum(truth["eligible_agent_scoring"] for truth in truths)
    step = sum(truth["eligible_step_scoring"] for truth in truths)
    joint = sum(
        truth["eligible_agent_scoring"] and truth["eligible_step_scoring"] for truth in truths
    )
    return {
        "total": len(rows),
        "agent_eligible": agent,
        "agent_excluded": len(rows) - agent,
        "step_eligible": step,
        "step_excluded": len(rows) - step,
        "joint_eligible": joint,
        "joint_excluded": len(rows) - joint,
        "annotation_anomalous_samples": sum(bool(t["annotation_anomaly"]) for t in truths),
    }


def regression_summary(rows: list[dict]) -> dict:
    total = len(rows)
    result: dict = {
        "total": total,
        "denominator": "all completed sample attempts, including stage failures",
    }
    for key in (
        "attribution_success",
        "synthesizable",
        "replay_compatible",
        "needs_review",
        "compiled",
        "end_to_end_success",
    ):
        count = sum(r.get(key) is True for r in rows)
        result[key] = {"count": count, "rate": fraction(count, total)}
    for verdict in ("valid", "not_reproduced", "not_discriminating", "inconclusive"):
        count = sum(r.get("verdict") == verdict for r in rows)
        result[verdict] = {"count": count, "rate": fraction(count, total)}
    result["review_required"] = sum(
        r.get("needs_review") is True or r.get("abstained") is True for r in rows
    )
    result["stage_failures"] = dict(
        sorted(Counter(r["failure"]["stage"] for r in rows if r.get("failure")).items())
    )
    return result


def build_report(rows: list[dict], track: str, *, planned: int, usages: list[dict]) -> dict:
    summary = attribution_summary if track == "attribution" else regression_summary
    result = {
        "track": track,
        "planned_samples": planned,
        "completed_samples": len(rows),
        "remaining_samples": planned - len(rows),
        "metrics": summary(rows),
        "usage": usage_summary(usages),
    }
    wall = [row["wall_latency_ms"] for row in rows if row.get("wall_latency_ms") is not None]
    result["sample_wall_latency_ms"] = {
        "mean": mean(wall) if wall else None,
        "median": median(wall) if wall else None,
        "known_samples": len(wall),
    }
    if track == "attribution":
        result["eligibility"] = eligibility_summary(rows)
        result["by_subset"] = {
            subset: summary([r for r in rows if r["subset"] == subset])
            for subset in sorted({r["subset"] for r in rows})
        }
        result["by_context"] = {
            name: summary(
                [r for r in rows if r.get("context") and r["context"]["full_trace_used"] == full]
            )
            for name, full in (("full", True), ("pruned", False))
        }
        result["by_truncated_evidence"] = {
            str(truncated): summary(
                [
                    r
                    for r in rows
                    if r.get("context") and bool(r["context"]["truncated_sequences"]) == truncated
                ]
            )
            for truncated in (False, True)
        }
        result["context"] = context_summary(rows)
        result["by_decisive_step_retention"] = {
            name: summary([row for row in rows if row.get("decisive_step_retention") == name])
            for name in ("complete", "truncated", "omitted")
        }
        result["anomalies"] = dict(
            sorted(
                Counter(
                    anomaly
                    for r in rows
                    if r.get("ground_truth")
                    for anomaly in r["ground_truth"]["annotation_anomaly"]
                ).items()
            )
        )
    result["interpretation"] = (
        "Descriptive measurements; context correlations do not establish causation."
    )
    return result
