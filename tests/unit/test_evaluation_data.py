import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.core.context import ContextPolicy
from app.schemas.attributions import AttributionDecision, EvidenceReference
from app.services.attribution_input import build_attribution_request
from app.services.context import ContextManager
from evaluation.artifacts import human_report, serialized
from evaluation.metrics import (
    attribution_summary,
    context_summary,
    regression_summary,
    score,
    usage_summary,
)
from evaluation.who_when import (
    audit_dataset,
    build_manifests,
    labels,
    load_dataset,
    related_groups,
    retention,
    samples_from_rows,
    validate_manifest,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/who_when"


def raw_record(**changes):
    return {
        "question": "Find the recorded failure.",
        "question_ID": "q1",
        "mistake_agent": "Worker",
        "mistake_step": "1",
        "mistake_reason": "PRIVATE_ORACLE_REASON",
        "ground_truth": "PRIVATE_ANSWER",
        "history": [
            {"role": "system", "name": "Manager", "content": ""},
            {"role": "user", "name": "Worker", "content": "incorrect"},
        ],
    } | changes


def sample(**changes):
    return samples_from_rows("Algorithm-Generated", [raw_record(**changes)])[0]


def prediction(agent="Worker", sequence=1, *, abstain=False, confidence="high"):
    return AttributionDecision(
        decision="abstain" if abstain else "attributed",
        responsible_agent=None if abstain else agent,
        decisive_sequence=None if abstain else sequence,
        failure_category=None,
        explanation="Visible evidence is sufficient.",
        confidence=confidence,
        evidence=()
        if abstain
        else (
            EvidenceReference(start_sequence=sequence, end_sequence=sequence, reason="Observed"),
        ),
        abstention_reason="Insufficient evidence" if abstain else None,
    )


@pytest.mark.parametrize(
    "subset,variant",
    [("Algorithm-Generated", "algorithm_generated"), ("Hand-Crafted", "hand_crafted")],
)
def test_real_fixture_loading_preserves_labels_and_history_outside_inference(subset, variant):
    records = json.loads((FIXTURES / f"{variant}_sample.json").read_text(encoding="utf-8"))
    original = deepcopy(records)
    samples = samples_from_rows(subset, records)
    assert len(samples) == 5
    for item in samples:
        trace = item.canonical()
        context = ContextManager(ContextPolicy()).build(trace)
        request = build_attribution_request(context, trace, model="test", reasoning_effort="medium")
        assert "mistake_agent" not in request.evidence_json
        assert "mistake_step" not in request.evidence_json
        assert "question_ID" not in request.evidence_json
        assert len(trace.steps) == len(json.loads(item.record["history"]))
    assert records == original


def test_local_parquet_loader_roundtrip(tmp_path):
    arrow = pytest.importorskip("pyarrow")
    parquet = pytest.importorskip("pyarrow.parquet")
    for subset in ("Algorithm-Generated", "Hand-Crafted"):
        record = raw_record()
        if subset == "Hand-Crafted":
            record["history"] = [
                {"role": "human", "content": ""},
                {"role": "Worker", "content": "incorrect"},
            ]
        parquet.write_table(arrow.Table.from_pylist([record]), tmp_path / f"{subset}.parquet")
    records = load_dataset(tmp_path)
    assert len(records) == 2
    assert len({r.sample_id for r in records}) == 2
    assert records[1].canonical().steps[0].agent_id is None


@pytest.mark.parametrize("value", [None, "", "1.0", "01", " 1", "-1", True, 1.0, "unknown"])
def test_invalid_step_annotations_are_not_repaired(value):
    item = sample(mistake_step=value)
    truth = labels(item, item.canonical())
    assert not truth["eligible_step_scoring"]
    assert truth["raw"]["mistake_step"] == value
    assert truth["eligible_agent_scoring"]


@pytest.mark.parametrize("value", [2, "2", 10000])
def test_out_of_range_annotation_does_not_fail_trace_processing(value):
    rows = audit_dataset([sample(mistake_step=value)], ContextManager(ContextPolicy()))
    assert rows[0]["failure"] is None
    assert rows[0]["context"]["full_trace_used"]
    assert "step_out_of_range" in rows[0]["ground_truth"]["annotation_anomaly"]
    assert rows[0]["decisive_step_retention"] is None


def test_case_variants_remain_raw_and_ineligible_but_independent_step_is_valid():
    item = sample(mistake_agent="worker")
    truth = labels(item, item.canonical())
    assert truth["raw"]["mistake_agent"] == "worker"
    assert truth["scoring_agent"] is None
    assert not truth["eligible_agent_scoring"]
    assert truth["eligible_step_scoring"]
    assert "agent_casing_variant" in truth["annotation_anomaly"]
    assert "agent_step_speaker_disagreement" in truth["annotation_anomaly"]


def test_index_zero_and_system_empty_messages_count():
    item = sample(mistake_agent="Manager", mistake_step="0")
    truth = labels(item, item.canonical())
    assert truth["scoring_sequence"] == 0
    assert item.canonical().steps[0].output == ""
    assert score(prediction("Manager", 0), truth)["joint_correct"] is True


def test_label_mutation_cannot_change_attribution_request():
    item = sample()
    other = sample(
        mistake_agent="PRIVATE_AGENT",
        mistake_step="123456",
        ground_truth="OTHER_PRIVATE_ANSWER",
        mistake_reason="OTHER_PRIVATE_REASON",
        question_ID="other",
        mistake_type="secret",
    )
    requests = []
    for value in (item, other):
        trace = value.canonical()
        requests.append(
            build_attribution_request(
                ContextManager(ContextPolicy()).build(trace),
                trace,
                model="test",
                reasoning_effort="medium",
            )
        )
    assert requests[0] == requests[1]
    assert "PRIVATE" not in requests[0].evidence_json


def test_measurement_context_matches_production_envelope_without_label_dependent_ids():
    from evaluation.context import analysis_context

    first = sample()
    changed = sample(mistake_agent="private", mistake_step="9999")
    manager = ContextManager(ContextPolicy())
    context = analysis_context(manager, first.canonical())
    assert context.run_id is not None
    assert context == analysis_context(manager, changed.canonical())
    assert (
        context.statistics.estimated_selected_size
        > manager.build(first.canonical()).statistics.estimated_selected_size
    )


def test_duplicates_have_stable_distinct_sample_ids_and_are_anomalies():
    records = samples_from_rows("Algorithm-Generated", [raw_record(), raw_record()])
    assert records[0].sample_id != records[1].sample_id
    assert records == samples_from_rows("Algorithm-Generated", [raw_record(), raw_record()])
    audits = audit_dataset(records, ContextManager(ContextPolicy()))
    assert "duplicate_question_id_within_subset" in audits[0]["ground_truth"]["annotation_anomaly"]


def test_grouping_is_transitive_across_subsets_and_identical_questions():
    records = samples_from_rows(
        "Algorithm-Generated",
        [
            raw_record(question_ID="a", question="first"),
            raw_record(question_ID="b", question="second"),
            raw_record(question_ID="c", question="separate"),
        ],
    )
    records += samples_from_rows("Hand-Crafted", [raw_record(question_ID="A", question="SECOND")])
    groups = related_groups(records)
    assert (
        groups[records[0].sample_id] == groups[records[1].sample_id] == groups[records[3].sample_id]
    )
    assert groups[records[2].sample_id] != groups[records[0].sample_id]


def test_manifests_are_deterministic_and_fixture_exposure_cannot_enter_final(tmp_path):
    records = samples_from_rows(
        "Algorithm-Generated",
        [raw_record(question_ID=str(i), question=f"task {i}") for i in range(10)],
    )
    (tmp_path / "exposed.json").write_text(json.dumps([records[-1].record]), encoding="utf-8")
    audits = audit_dataset(records, ContextManager(ContextPolicy()))
    manifest = build_manifests(records, audits, fixture_dir=tmp_path, development_count=3)
    other = build_manifests(
        list(reversed(records)), list(reversed(audits)), fixture_dir=tmp_path, development_count=3
    )
    assert serialized(manifest) == serialized(other)
    assert (
        next(m for m in manifest["members"] if m["sample_id"] == records[-1].sample_id)["partition"]
        == "development"
    )
    validate_manifest(manifest, records)
    manifest["members"][0]["input_digest"] = "bad"
    with pytest.raises(ValueError, match="differs"):
        validate_manifest(manifest, records)


def test_leaking_group_manifest_is_rejected(tmp_path):
    records = samples_from_rows("Algorithm-Generated", [raw_record(), raw_record(question="other")])
    manifest = build_manifests(
        records,
        audit_dataset(records, ContextManager(ContextPolicy())),
        fixture_dir=tmp_path,
        development_count=1,
    )
    manifest["members"][0]["partition"] = "final"
    with pytest.raises(ValueError, match="cross"):
        validate_manifest(manifest, records)


def test_agent_step_joint_and_abstention_metrics_use_explicit_denominators():
    item = sample()
    truth = labels(item, item.canonical())
    decisions = [prediction(), prediction("Manager", 0), prediction(abstain=True), prediction()]
    rows = [
        {
            "scores": score(p, truth, valid=i != 3),
            "prediction": p.model_dump(mode="json"),
            "failure": {"code": "invalid"} if i == 3 else None,
            "invalid_semantic_output": i == 3,
        }
        for i, p in enumerate(decisions)
    ]
    result = attribution_summary(rows)
    assert (
        result["agent"]["accuracy"]
        == result["step"]["accuracy"]
        == result["joint"]["accuracy"]
        == 0.25
    )
    assert result["coverage"] == 0.5
    assert result["abstention_rate"] == 0.25
    assert result["covered_agent"]["accuracy"] == 0.5
    assert result["invalid_semantic_output_rate"] == 0.25
    assert result["confidence_distribution"]["high"] == 3
    assert attribution_summary([])["agent"]["accuracy"] is None


def test_step_scoring_is_equality_not_substring_and_agent_scoring_is_case_sensitive():
    truth = {
        "eligible_agent_scoring": True,
        "scoring_agent": "Worker",
        "eligible_step_scoring": True,
        "scoring_sequence": 1,
    }
    assert score(prediction("worker", 10), truth)["agent_correct"] is False
    assert score(prediction("Worker", 10), truth)["step_correct"] is False
    assert score(prediction("Manager", 1), truth)["step_correct"] is True
    assert score(prediction("Manager", 1), truth)["joint_correct"] is False


def test_retention_requires_complete_content_and_counts_context_failures():
    truth = {"eligible_step_scoring": True, "scoring_sequence": 1}
    context = {"visible_sequences": [0, 1], "truncated_sequences": [1]}
    assert retention(truth, context) == "truncated"
    context["visible_sequences"] = [0]
    assert retention(truth, context) == "omitted"
    rows = [
        {"ground_truth": truth, "context": None, "decisive_step_retention": v}
        for v in ("complete", "truncated", "omitted", None)
    ]
    result = context_summary(rows)
    assert result["decisive_step_retention_rate"] == 0.25
    assert result["decisive_sequence_visible_rate"] == 0.5
    assert result["decisive_context_unavailable"] == 1


def test_regression_metrics_do_not_confuse_compilation_with_success():
    rows = [
        {
            "compiled": True,
            "synthesizable": True,
            "end_to_end_success": False,
            "verdict": "not_discriminating",
        },
        {"compiled": True, "synthesizable": True, "end_to_end_success": True, "verdict": "valid"},
        {"needs_review": True},
        {"failure": {"stage": "attribution_provider"}},
        {"verdict": "inconclusive"},
    ]
    result = regression_summary(rows)
    assert result["compiled"]["rate"] == 2 / 5
    assert result["end_to_end_success"]["rate"] == 1 / 5
    assert result["inconclusive"]["count"] == result["review_required"] == 1
    assert result["stage_failures"] == {"attribution_provider": 1}


def test_usage_preserves_unknowns_and_does_not_invent_billed_cost():
    result = usage_summary(
        [
            {"input_tokens": 10, "output_tokens": 5, "reasoning_tokens": 2, "latency_ms": 20},
            {"input_tokens": None, "latency_ms": 40},
        ]
    )
    assert result["input_tokens"] == {"total": 10, "known_responses": 1}
    assert result["provider_cost"] is None
    assert result["provider_latency_ms"]["mean"] == 30
    assert usage_summary([])["input_tokens"]["total"] is None
    report = {"track": "test", "metrics": {"agent": {"correct": 1, "eligible": 2, "accuracy": 0.5}}}
    assert human_report(report) == human_report(deepcopy(report))
