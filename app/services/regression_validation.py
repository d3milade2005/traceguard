import json
from dataclasses import dataclass

from pydantic import JsonValue

from app.core.exceptions import RegressionValidationError
from app.regression_runtime import assertion_holds as runtime_assertion_holds
from app.regression_runtime import resolve
from app.schemas.attributions import AttributionRecord
from app.schemas.regression_specs import (
    Assertion,
    RegressionSpec,
    ScenarioSnapshot,
    SourceReference,
    check_data,
)
from app.schemas.traces import CanonicalTrace


@dataclass(frozen=True)
class RegressionEvidence:
    attribution: AttributionRecord
    canonical: CanonicalTrace
    sequences: frozenset[int]


def assertion_holds(assertion: Assertion, value: JsonValue) -> bool:
    try:
        return runtime_assertion_holds(assertion.kind, value, assertion.path, assertion.expected)
    except ValueError as exc:
        raise RegressionValidationError from exc


def source_value(ref: SourceReference, evidence: RegressionEvidence) -> JsonValue:
    if ref.sequence not in evidence.sequences:
        raise RegressionValidationError
    present, value = resolve(getattr(evidence.canonical.steps[ref.sequence], ref.field), ref.path)
    if not present:
        raise RegressionValidationError
    return value


def validate_regression(
    spec: RegressionSpec, evidence: RegressionEvidence, *, max_scenario_bytes: int
) -> ScenarioSnapshot | None:
    """Validate provenance and failure-catching semantics, without running agent code."""
    attribution = evidence.attribution
    if attribution.status != "completed":
        raise RegressionValidationError
    if spec.target_agent != attribution.responsible_agent:
        raise RegressionValidationError
    for citation in (*spec.evidence, *(c for a in spec.assertions for c in a.evidence)):
        if citation.sequence not in evidence.sequences:
            raise RegressionValidationError
        value = (
            evidence.canonical.task
            if citation.field == "task"
            else getattr(evidence.canonical.steps[citation.sequence], citation.field)
        )
        # Match the exact compact representation supplied to the provider, while
        # retaining compatibility with persisted specifications using spaced JSON.
        texts = (
            (value,)
            if isinstance(value, str)
            else (
                json.dumps(value, ensure_ascii=False),
                json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
        )
        if not citation.quote.strip() or not any(citation.quote in text for text in texts):
            raise RegressionValidationError
    if spec.disposition == "needs_review":
        return None
    if attribution.decision != "attributed" or attribution.decisive_sequence is None:
        raise RegressionValidationError
    if any(reference.contains_truncated for reference in attribution.evidence):
        raise RegressionValidationError
    decisive = attribution.decisive_sequence
    if decisive not in evidence.sequences or not any(c.sequence == decisive for c in spec.evidence):
        raise RegressionValidationError
    scenario = spec.scenario
    assert scenario is not None
    stimulus = scenario.stimulus
    if (
        stimulus.sequence != decisive
        or stimulus.field not in {"input", "tool_arguments"}
        or stimulus.path
        or evidence.canonical.steps[decisive].agent_id != spec.target_agent
    ):
        raise RegressionValidationError
    value = source_value(stimulus, evidence)
    if value is None:
        raise RegressionValidationError
    if len(set(scenario.preconditions)) != len(scenario.preconditions):
        raise RegressionValidationError
    preconditions: list[JsonValue] = []
    for ref in scenario.preconditions:
        if ref.sequence >= decisive:
            raise RegressionValidationError
        preconditions.append(source_value(ref, evidence))
    snapshot = ScenarioSnapshot(stimulus=value, preconditions=tuple(preconditions))
    try:
        check_data(snapshot.model_dump(mode="json"))
    except ValueError as exc:
        raise RegressionValidationError from exc
    if len(snapshot.model_dump_json().encode()) > max_scenario_bytes:
        raise RegressionValidationError
    catches = False
    for assertion in spec.assertions:
        if not any(c.sequence == decisive for c in assertion.evidence):
            raise RegressionValidationError
        if assertion.kind not in {"exists", "not_exists"}:
            operand = assertion.expected
            text = operand if isinstance(operand, str) else json.dumps(operand)
            if not any(text in citation.quote for citation in assertion.evidence):
                raise RegressionValidationError
        catches |= not assertion_holds(
            assertion, getattr(evidence.canonical.steps[decisive], assertion.observable)
        )
    if not catches:
        raise RegressionValidationError
    return snapshot
