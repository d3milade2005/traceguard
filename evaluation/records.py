"""Adapt actual model results to the existing compiler/service contracts, without a DB."""

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from app.schemas.attributions import AttributionRecord, LLMResult
from app.schemas.context import AnalysisContext
from app.schemas.regression_specs import RegressionRecord, RegressionResult, ScenarioSnapshot
from app.services.attribution_input import PROMPT_VERSION, context_manifest
from app.services.regression_input import PROMPT_VERSION as REGRESSION_PROMPT_VERSION

EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


def attribution_record(
    sample_id: str, result: LLMResult, context: AnalysisContext, *, model: str, effort: str
) -> AttributionRecord:
    decision = result.attribution.model_dump(mode="json")
    truncated = set(context_manifest(context)["truncated_sequences"])
    decision["evidence"] = [
        ref.model_dump(mode="json")
        | {
            "contains_truncated": any(
                ref.start_sequence <= seq <= ref.end_sequence for seq in truncated
            )
        }
        for ref in result.attribution.evidence
    ]
    return AttributionRecord.model_validate(
        {
            **decision,
            **result.usage.model_dump(mode="json"),
            "id": uuid5(NAMESPACE_URL, sample_id + "/attribution"),
            "run_id": uuid5(NAMESPACE_URL, sample_id),
            "status": "completed",
            "provider": "openai",
            "model": model,
            "reasoning_effort": effort,
            "prompt_version": PROMPT_VERSION,
            "input_sha256": None,
            "context_manifest": context_manifest(context),
            "failure_code": None,
            "failed_at": None,
            "completed_at": EPOCH,
            "created_at": EPOCH,
            "updated_at": EPOCH,
        }
    )


def regression_record(
    sample_id: str, result: RegressionResult, snapshot: ScenarioSnapshot, *, model: str, effort: str
) -> RegressionRecord:
    spec = result.spec
    return RegressionRecord.model_validate(
        {
            "id": uuid5(NAMESPACE_URL, sample_id + "/regression"),
            "attribution_id": uuid5(NAMESPACE_URL, sample_id + "/attribution"),
            "status": "completed",
            "disposition": spec.disposition,
            "version": spec.version,
            "title": spec.title,
            "target_agent": spec.target_agent,
            "review_reason": spec.review_reason,
            "spec": spec,
            "scenario_snapshot": snapshot,
            "provider": "openai",
            "model": model,
            "reasoning_effort": effort,
            "prompt_version": REGRESSION_PROMPT_VERSION,
            **result.usage.model_dump(mode="json"),
            "input_sha256": None,
            "failure_code": None,
            "failed_at": None,
            "completed_at": EPOCH,
            "created_at": EPOCH,
            "updated_at": EPOCH,
        }
    )
