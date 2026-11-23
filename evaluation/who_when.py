"""Who&When loading, annotation eligibility, context audit, and grouped manifests."""

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from app.core.exceptions import ApplicationError
from app.integrations.traces.who_when import WhoWhenTraceAdapter, WhoWhenVariant
from app.schemas.traces import CanonicalTrace
from app.services.context import ContextManager
from evaluation.artifacts import digest
from evaluation.context import analysis_context, context_statistics

SEMANTICS_VERSION = "whowhen-official-zero-based-v1"
SCORING_VERSION = "strict-exact-no-agent-normalization-v1"
SUBSETS: dict[str, WhoWhenVariant] = {
    "Algorithm-Generated": "algorithm_generated",
    "Hand-Crafted": "hand_crafted",
}


@dataclass(frozen=True)
class Sample:
    sample_id: str
    subset: str
    row: int
    record: dict[str, JsonValue]

    def inference_input(self) -> dict[str, JsonValue]:
        # A positive allowlist, rather than deleting an evolving list of annotations.
        return {"question": self.record.get("question"), "history": self.record.get("history")}

    def canonical(self) -> CanonicalTrace:
        return WhoWhenTraceAdapter(variant=SUBSETS[self.subset]).adapt(self.inference_input())


def samples_from_rows(subset: str, rows: list[dict[str, JsonValue]]) -> list[Sample]:
    if subset not in SUBSETS:
        raise ValueError("Unknown Who&When subset")
    duplicates: Counter[str] = Counter()
    samples = []
    for index, row in enumerate(rows):
        fingerprint = digest(row)
        occurrence = duplicates[fingerprint]
        duplicates[fingerprint] += 1
        samples.append(Sample(f"{subset}/{fingerprint}/{occurrence}", subset, index, row))
    return samples


def load_dataset(root: Path) -> list[Sample]:
    # Arrow is a research-only optional dependency, absent from the runtime application.
    import pyarrow.parquet as parquet

    samples = []
    for subset in SUBSETS:
        rows = parquet.read_table(root / f"{subset}.parquet").to_pylist()
        samples.extend(samples_from_rows(subset, rows))
    return samples


def labels(sample: Sample, trace: CanonicalTrace) -> dict:
    raw_agent = sample.record.get("mistake_agent")
    raw_step = sample.record.get("mistake_step")
    agents = {step.agent_id for step in trace.steps if step.agent_id is not None}
    anomalies: list[str] = []
    agent = raw_agent if isinstance(raw_agent, str) and raw_agent.strip() else None
    if agent is None:
        anomalies.append("missing_or_invalid_agent")
    elif agent not in agents:
        anomalies.append(
            "agent_casing_variant"
            if any(agent.casefold() == a.casefold() for a in agents)
            else "agent_not_in_trace"
        )
    sequence = None
    if type(raw_step) is int:
        sequence = raw_step
    elif isinstance(raw_step, str) and re.fullmatch(r"0|[1-9][0-9]*", raw_step):
        sequence = int(raw_step)
    else:
        anomalies.append("missing_or_invalid_step")
    if sequence is not None and not 0 <= sequence < len(trace.steps):
        anomalies.append("step_out_of_range")
        sequence = None
    if sequence is not None and agent is not None and trace.steps[sequence].agent_id != agent:
        anomalies.append("agent_step_speaker_disagreement")
    if (
        not isinstance(sample.record.get("question_ID"), str)
        or not str(sample.record.get("question_ID", "")).strip()
    ):
        anomalies.append("missing_or_invalid_question_id")
    nullable = sorted(key for key, value in sample.record.items() if value is None)
    expected_fields = {
        "question",
        "history",
        "question_ID",
        "mistake_agent",
        "mistake_step",
        "mistake_reason",
    } | (
        {"ground_truth", "is_correct"}
        if sample.subset == "Algorithm-Generated"
        else {"groundtruth", "is_corrected", "mistake_type"}
    )
    missing = sorted(expected_fields - sample.record.keys())
    if missing:
        anomalies.append("missing_fields")
    if nullable:
        anomalies.append("nullable_fields")
    # Casing variants cannot be matched by the production contract. Preserve and exclude
    # them from agent scoring; valid independent steps can still be scored.
    return {
        "raw": {
            key: sample.record.get(key)
            for key in (
                "mistake_agent",
                "mistake_step",
                "mistake_reason",
                "mistake_type",
                "ground_truth",
                "groundtruth",
                "is_correct",
                "is_corrected",
                "question_ID",
            )
        },
        "scoring_agent": agent if agent in agents else None,
        "scoring_sequence": sequence,
        "eligible_agent_scoring": agent in agents,
        "eligible_step_scoring": sequence is not None,
        "annotation_anomaly": sorted(anomalies),
        "nullable_fields": nullable,
        "missing_fields": missing,
    }


def retention(ground_truth: dict, context: dict) -> str | None:
    sequence = ground_truth["scoring_sequence"]
    if not ground_truth["eligible_step_scoring"]:
        return None
    if sequence not in context["visible_sequences"]:
        return "omitted"
    return "truncated" if sequence in context["truncated_sequences"] else "complete"


def audit_sample(sample: Sample, manager: ContextManager) -> dict:
    result = {
        "sample_id": sample.sample_id,
        "subset": sample.subset,
        "row": sample.row,
        "record_digest": digest(sample.record),
        "input_digest": digest(sample.inference_input()),
    }
    try:
        trace = sample.canonical()
    except ApplicationError as exc:
        return result | {
            "failure": {"stage": "adapter", "code": exc.code},
            "context": None,
            "ground_truth": None,
            "canonical_step_count": None,
        }
    truth = labels(sample, trace)
    result |= {"ground_truth": truth, "canonical_step_count": len(trace.steps)}
    try:
        context = context_statistics(analysis_context(manager, trace))
    except ApplicationError as exc:
        return result | {"failure": {"stage": "context", "code": exc.code}, "context": None}
    return result | {
        "failure": None,
        "context": context,
        "decisive_step_retention": retention(truth, context),
    }


def audit_dataset(samples: list[Sample], manager: ContextManager) -> list[dict]:
    rows = [audit_sample(sample, manager) for sample in samples]
    ids: dict[str, list[Sample]] = {}
    for sample in samples:
        value = sample.record.get("question_ID")
        if isinstance(value, str) and value:
            ids.setdefault(value, []).append(sample)
    for sample, row in zip(samples, rows, strict=True):
        truth = row["ground_truth"]
        related = ids.get(str(sample.record.get("question_ID")), [])
        if truth is not None and len(related) > 1:
            truth["annotation_anomaly"].append("overlapping_question_id")
            if sum(other.subset == sample.subset for other in related) > 1:
                truth["annotation_anomaly"].append("duplicate_question_id_within_subset")
            truth["annotation_anomaly"].sort()
    return rows


def related_groups(samples: list[Sample]) -> dict[str, str]:
    """Transitive union of question IDs and question text, across both subsets."""
    parents = {sample.sample_id: sample.sample_id for sample in samples}

    def find(value: str) -> str:
        while value != parents[value]:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    seen: dict[tuple[str, str], str] = {}
    for sample in sorted(samples, key=lambda s: s.sample_id):
        for field in ("question_ID", "question"):
            value = sample.record.get(field)
            if not isinstance(value, str) or not value.strip():
                continue
            # Conservative grouping only; this is never a scoring normalization.
            key = field, " ".join(value.casefold().split())
            if key in seen:
                left, right = find(sample.sample_id), find(seen[key])
                parents[max(left, right)] = min(left, right)
            seen[key] = sample.sample_id
    return {sid: digest(find(sid)) for sid in parents}


def build_manifests(
    samples: list[Sample], audits: list[dict], *, fixture_dir: Path, development_count: int = 16
) -> dict:
    groups = related_groups(samples)
    exposed_ids: set[str] = set()
    exposed_questions: set[str] = set()
    for path in sorted(fixture_dir.glob("*.json")):
        for record in json.loads(path.read_text(encoding="utf-8")):
            exposed_ids.add(str(record.get("question_ID")))
            exposed_questions.add(str(record.get("question")))
    dev_groups = {
        groups[s.sample_id]
        for s in samples
        if str(s.record.get("question_ID")) in exposed_ids
        or str(s.record.get("question")) in exposed_questions
    }
    candidates = [
        a
        for a in audits
        if a["context"] is not None
        and a["ground_truth"]
        and a["ground_truth"]["eligible_agent_scoring"]
    ]
    chosen: list[str] = []
    covered: set[tuple] = set()

    def features(a: dict) -> set[tuple]:
        return {
            ("subset", a["subset"]),
            ("context", a["context"]["full_trace_used"]),
            ("length", a["canonical_step_count"] > 10),
            ("agent", a["ground_truth"]["scoring_agent"]),
            ("anomaly", bool(a["ground_truth"]["annotation_anomaly"])),
        }

    # Fixed greedy coverage, with digest ordering as the deterministic tie breaker.
    # Selection occurs before any prediction and never consults experimental scores.
    while candidates and len(chosen) < development_count:
        candidates.sort(key=lambda a: (-len(features(a) - covered), digest(a["sample_id"])))
        item = candidates.pop(0)
        chosen.append(item["sample_id"])
        covered |= features(item)
        dev_groups.add(groups[item["sample_id"]])
    members = [
        {
            "sample_id": s.sample_id,
            "record_digest": digest(s.record),
            "input_digest": digest(s.inference_input()),
            "group_id": groups[s.sample_id],
            "partition": "development" if groups[s.sample_id] in dev_groups else "final",
            "subset": s.subset,
        }
        for s in sorted(samples, key=lambda s: s.sample_id)
    ]
    return {
        "version": "whowhen-grouped-v1",
        "semantics_version": SEMANTICS_VERSION,
        "scoring_version": SCORING_VERSION,
        "dataset_digest": digest(sorted((s.sample_id, digest(s.record)) for s in samples)),
        "grouping": "transitive casefold/whitespace-normalized question_ID OR question",
        "fixture_exposed_groups_reserved_for_development": True,
        "members": members,
        "development_sample": chosen,
    }


def validate_manifest(manifest: dict, samples: list[Sample]) -> None:
    if (
        manifest.get("version") != "whowhen-grouped-v1"
        or manifest.get("semantics_version") != SEMANTICS_VERSION
        or manifest.get("scoring_version") != SCORING_VERSION
        or manifest.get("dataset_digest")
        != digest(sorted((s.sample_id, digest(s.record)) for s in samples))
    ):
        raise ValueError("Manifest dataset digest, scoring, or semantics differ")
    expected = {s.sample_id: s for s in samples}
    members = manifest["members"]
    if (
        len({m["sample_id"] for m in members}) != len(members)
        or {m["sample_id"] for m in members} != expected.keys()
    ):
        raise ValueError("Manifest must contain each dataset record exactly once")
    groups = related_groups(samples)
    partitions: dict[str, set[str]] = {}
    for member in members:
        sample = expected[member["sample_id"]]
        if (
            member["record_digest"] != digest(sample.record)
            or member["input_digest"] != digest(sample.inference_input())
            or member["group_id"] != groups[sample.sample_id]
            or member["partition"] not in {"development", "final"}
        ):
            raise ValueError("Dataset or manifest differs from the frozen split")
        partitions.setdefault(member["group_id"], set()).add(member["partition"])
    if any(len(values) != 1 for values in partitions.values()):
        raise ValueError("Related questions cross development/final partitions")
    dev = {m["sample_id"] for m in members if m["partition"] == "development"}
    if (
        len(set(manifest["development_sample"])) != len(manifest["development_sample"])
        or not set(manifest["development_sample"]) <= dev
    ):
        raise ValueError("Development sample is invalid")
