"""Track A invokes the existing production input and validation boundaries."""

import time

from app.core.exceptions import ApplicationError, AttributionValidationError
from app.integrations.llm.contracts import LLMClient
from app.schemas.attributions import LLMResult, ReasoningEffort
from app.services.attribution_input import build_attribution_request, input_digest
from app.services.attribution_validation import validate_attribution
from app.services.context import ContextManager
from evaluation.artifacts import Experiment, RecordedCallFailure, safe_failure
from evaluation.context import analysis_context, context_statistics
from evaluation.metrics import score
from evaluation.who_when import Sample, labels, retention


def evaluate_sample(
    sample: Sample,
    manager: ContextManager,
    client: LLMClient,
    experiment: Experiment,
    *,
    model: str,
    effort: ReasoningEffort,
    max_calls: int,
) -> dict:
    started = time.monotonic()
    stage = "adapter"
    prediction = None
    context = None
    trace = None
    failure = None
    invalid = False
    usage = None
    request_digest = None
    try:
        trace = sample.canonical()
        stage = "context"
        selected = analysis_context(manager, trace)
        context = context_statistics(selected)
        stage = "attribution_input"
        request = build_attribution_request(selected, trace, model=model, reasoning_effort=effort)
        request_digest = input_digest(request)
        stage = "attribution_provider"
        result = experiment.call(
            sample.sample_id,
            "attribution",
            request,
            LLMResult,
            lambda: client.attribute(request),
            max_calls=max_calls,
        )
        prediction = result.attribution
        usage = result.usage.model_dump(mode="json")
        stage = "attribution_validation"
        validate_attribution(prediction, selected, trace)
    except RecordedCallFailure as exc:
        failure = exc.failure | {"stage": stage}
        usage = failure.get("usage")
    except ApplicationError as exc:
        failure = safe_failure(exc) | {"stage": stage}
        invalid = isinstance(exc, AttributionValidationError)
    # Labels are consulted only after prediction/attempt termination. They never
    # enter the client, request builder, context selector, or production service.
    truth = (
        labels(sample, trace)
        if trace is not None
        else {
            "raw": {k: sample.record.get(k) for k in ("mistake_agent", "mistake_step")},
            "eligible_agent_scoring": False,
            "eligible_step_scoring": False,
            "scoring_agent": None,
            "scoring_sequence": None,
            "annotation_anomaly": ["trace_unadaptable"],
        }
    )
    return {
        "subset": sample.subset,
        "row": sample.row,
        "input_digest": request_digest,
        "prediction": prediction.model_dump(mode="json") if prediction else None,
        "ground_truth": truth,
        "context": context,
        "decisive_step_retention": retention(truth, context) if context else None,
        "scores": score(prediction, truth, valid=failure is None),
        "usage": usage,
        "wall_latency_ms": round((time.monotonic() - started) * 1000),
        "failure": failure,
        "invalid_semantic_output": invalid,
    }
