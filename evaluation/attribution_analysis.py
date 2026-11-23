"""Exact-score verification, descriptive uncertainty and transparent usage estimates."""

import json
import random
from collections import Counter, defaultdict
from decimal import Decimal
from math import sqrt
from pathlib import Path
from statistics import NormalDist

from app.core.context import ContextPolicy
from app.schemas.attributions import AttributionDecision
from app.services.attribution_input import build_attribution_request, input_digest
from app.services.attribution_validation import validate_attribution
from app.services.context import ContextManager
from evaluation.artifacts import digest, read_json
from evaluation.context import analysis_context, context_statistics
from evaluation.metrics import attribution_summary, score
from evaluation.preparation import PRICING
from evaluation.who_when import Sample, labels, validate_manifest


def wilson_interval(correct: int, eligible: int) -> list[float] | None:
    if not eligible:
        return None
    z = NormalDist().inv_cdf(0.975)
    p = correct / eligible
    scale = 1 + z * z / eligible
    centre = (p + z * z / (2 * eligible)) / scale
    margin = z * sqrt(p * (1 - p) / eligible + z * z / (4 * eligible**2)) / scale
    return [
        0.0 if correct == 0 else max(0, centre - margin),
        1.0 if correct == eligible else min(1, centre + margin),
    ]


def cluster_interval(
    rows: list[dict], groups: dict[str, str], key: str, *, repetitions: int = 5000
) -> list[float] | None:
    clusters = defaultdict(list)
    for row in rows:
        clusters[groups[row["sample_id"]]].append(row["scores"][key])
    values = list(clusters.values())
    if len(values) < 2 or not any(v is not None for cluster in values for v in cluster):
        return None
    rng = random.Random(20261123)
    estimates = []
    for _ in range(repetitions):
        draw = [
            v for cluster in rng.choices(values, k=len(values)) for v in cluster if v is not None
        ]
        if draw:
            estimates.append(sum(v is True for v in draw) / len(draw))
    estimates.sort()
    if not estimates:
        return None
    return [
        estimates[int(0.025 * (len(estimates) - 1))],
        estimates[int(0.975 * (len(estimates) - 1))],
    ]


def uncertainty(rows: list[dict], groups: dict[str, str]) -> dict:
    summary = attribution_summary(rows)
    return {
        "confidence_level": 0.95,
        "cluster_bootstrap_repetitions": 5000,
        "cluster_bootstrap_seed": 20261123,
        "clusters": len({groups[row["sample_id"]] for row in rows}),
        "interpretation": (
            "Conditional descriptive uncertainty on this partition. Cluster resampling accounts "
            "for related questions, not benchmark selection bias or model-call variability. "
            "Wilson intervals additionally assume independent cases."
        ),
        "accuracy": {
            name: {
                "cluster_bootstrap_percentile": cluster_interval(rows, groups, f"{name}_correct"),
                "wilson_casewise": wilson_interval(
                    summary[name]["correct"], summary[name]["eligible"]
                ),
            }
            for name in ("agent", "step", "joint")
        },
    }


def error_categories(rows: list[dict]) -> dict:
    counts = Counter()
    details = []
    for row in rows:
        scores = row["scores"]
        if row.get("failure"):
            category = "attempt_error"
        elif scores["abstained"]:
            category = "abstention"
        elif scores["joint_correct"] is None:
            category = "not_joint_scoring_eligible"
        elif scores["joint_correct"]:
            category = "agent_and_step_correct"
        elif scores["agent_correct"]:
            category = "correct_agent_wrong_step"
        elif scores["step_correct"]:
            category = "wrong_agent_correct_step"
        else:
            category = "agent_and_step_wrong"
        counts[category] += 1
        details.append(
            {
                "sample_id": row["sample_id"],
                "subset": row["subset"],
                "category": category,
                "ground_truth": row["ground_truth"],
                "prediction": row.get("prediction"),
                "failure": row.get("failure"),
                "decisive_step_retention": row.get("decisive_step_retention"),
                "context_incomplete": not (row.get("context") or {}).get("full_trace_used", False),
            }
        )
    return {
        "counts": dict(sorted(counts.items())),
        "cases": details,
        "causal_caution": (
            "Omitted evidence is an observed association, not proof that pruning caused error"
        ),
    }


def recorded_usage(root: Path, pricing: dict = PRICING) -> dict:
    intents = list((root / "calls").glob("*.intent.json"))
    entries = []
    for path in sorted((root / "calls").glob("*.provider.json")):
        event = read_json(path)
        body = event["response"]
        usage = body.get("usage")
        if usage is None:
            continue
        details = usage.get("input_tokens_details") or {}
        cached, written = details.get("cached_tokens"), details.get("cache_write_tokens")
        inputs, outputs = usage["input_tokens"], usage["output_tokens"]
        cache_read = cached if cached is not None else 0
        cache_write = written if written is not None else 0
        if min(inputs, outputs, cache_read, cache_write) < 0 or cache_read + cache_write > inputs:
            raise ValueError("Provider usage categories are inconsistent")
        input_factor = Decimal("2") if inputs > pricing["long_context_threshold"] else Decimal(1)
        output_factor = Decimal("1.5") if inputs > pricing["long_context_threshold"] else Decimal(1)
        plain = Decimal(pricing["input_per_million_usd"]) * input_factor
        read = Decimal(pricing["cached_input_per_million_usd"]) * input_factor
        write = Decimal(pricing["cache_write_per_million_usd"]) * input_factor
        output_price = Decimal(pricing["output_per_million_usd"]) * output_factor
        remainder = inputs - cache_read - cache_write
        base = cache_read * read + cache_write * write + outputs * output_price
        lower = base + remainder * (plain if cached is not None else read)
        upper = base + remainder * (plain if written is not None else write)
        if body.get("model") != pricing["model"] or body.get("service_tier") != "default":
            lower = upper = None
        entries.append(
            {
                "call_key": path.name.removesuffix(".provider.json"),
                "response_id": body.get("id"),
                "response_model": body.get("model"),
                "service_tier": body.get("service_tier"),
                "status": body.get("status"),
                "input_tokens": inputs,
                "output_tokens": outputs,
                "cached_input_tokens": cached,
                "cache_write_tokens": written,
                "reasoning_tokens": (usage.get("output_tokens_details") or {}).get(
                    "reasoning_tokens"
                ),
                "latency_ms": event.get("latency_ms"),
                "estimated_cost_lower_usd": str(lower / 1000000) if lower is not None else None,
                "estimated_cost_upper_usd": str(upper / 1000000) if upper is not None else None,
            }
        )
    estimates = [e for e in entries if e["estimated_cost_lower_usd"] is not None]
    return {
        "attempts_started": len(intents),
        "responses_with_usage": len(entries),
        "attempts_without_recorded_usage": len(intents) - len(entries),
        "input_tokens": sum(e["input_tokens"] for e in entries),
        "output_tokens": sum(e["output_tokens"] for e in entries),
        "reasoning_note": "Reasoning tokens are included in output tokens, never added again",
        "known_usage_estimate_lower_usd": str(
            sum((Decimal(e["estimated_cost_lower_usd"]) for e in estimates), Decimal(0))
        ),
        "known_usage_estimate_upper_usd": str(
            sum((Decimal(e["estimated_cost_upper_usd"]) for e in estimates), Decimal(0))
        ),
        "responses_with_price_estimate": len(estimates),
        "billing_verified": False,
        "cost_caution": (
            "Estimates cover recorded usage only. Missing usage is unknown cost, never zero. "
            "Missing cache details yield a range; mismatched model/tier yields unknown pricing."
        ),
        "pricing": pricing,
        "calls": entries,
    }


def verify_records(root: Path, manifest: dict, samples: list[Sample]) -> tuple[dict, list[dict]]:
    """Independently reconstruct saved inputs and scores before generating thesis tables."""
    config = read_json(root / "config.json")
    validate_manifest(manifest, samples)
    if config["manifest_digest"] != digest(manifest):
        raise ValueError("Experiment manifest differs")
    by_id = {s.sample_id: s for s in samples}
    expected = config["samples"]
    permitted = (
        [m["sample_id"] for m in manifest["members"] if m["partition"] == "final"]
        if config["partition"] == "final"
        else manifest["development_sample"]
    )
    if expected != permitted:
        raise ValueError("Experiment does not contain the complete declared partition/sample")
    files = list((root / "results").glob("*.json"))
    if len(files) != len(expected):
        raise ValueError(
            "Analysis requires all planned terminal records; partial runs stay incomplete"
        )
    manager = ContextManager(ContextPolicy.model_validate(config["inference"]["context_policy"]))
    rows = []
    for sid in expected:
        row = read_json(root / "results" / f"{digest(sid)}.json")
        if row["sample_id"] != sid or row["experiment_id"] != digest(config):
            raise ValueError("Result identity differs")
        sample = by_id[sid]
        trace = sample.canonical()
        context = analysis_context(manager, trace)
        truth = labels(sample, trace)
        request = build_attribution_request(
            context,
            trace,
            model=config["inference"]["model"],
            reasoning_effort=config["inference"]["reasoning_effort"],
        )
        if row["input_digest"] != input_digest(request) or row["context"] != context_statistics(
            context
        ):
            raise ValueError("Saved request/context differs from reconstruction")
        if row["ground_truth"] != truth:
            raise ValueError("Saved labels/eligibility differ")
        key = digest([sid, "attribution"])
        call = root / "calls" / key
        intent = read_json(call.with_suffix(".intent.json"))
        if intent["input_digest"] != digest(request.model_dump(mode="json")):
            raise ValueError("Journaled input digest differs")
        parsed_path = call.with_suffix(".response.json")
        if parsed_path.exists():
            parsed = read_json(parsed_path)
            if parsed["attribution"] != row["prediction"]:
                raise ValueError("Result prediction differs from the immutable response journal")
        elif row.get("failure") is None:
            raise ValueError("Successful result has no saved provider result")
        predicted = (
            AttributionDecision.model_validate_json(json.dumps(row["prediction"]))
            if row.get("prediction")
            else None
        )
        if row["scores"] != score(predicted, truth, valid=row.get("failure") is None):
            raise ValueError("Saved scores differ from independent calculation")
        if predicted is not None and row.get("failure") is None:
            validate_attribution(predicted, context, trace)
        rows.append(row)
    return config, rows
