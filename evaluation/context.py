"""Shared measurement of the unmodified production context-selection policy."""

from uuid import NAMESPACE_URL, uuid5

from app.schemas.context import AnalysisContext, TruncatedTraceStep
from app.schemas.traces import CanonicalTrace
from app.services.context import ContextManager
from evaluation.artifacts import digest

RUN_ID_POLICY = "uuid5-canonical-evidence-v1"


def analysis_context(manager: ContextManager, trace: CanonicalTrace) -> AnalysisContext:
    # Production includes a UUID in the budgeted envelope. Derive a reproducible
    # identifier from evidence alone; benchmark labels cannot influence it.
    run_id = uuid5(NAMESPACE_URL, digest(trace.model_dump(mode="json")))
    return manager.build(trace, run_id=run_id)


def context_statistics(context: AnalysisContext) -> dict:
    return context.statistics.model_dump(mode="json") | {
        "full_trace_used": context.full_trace_used,
        "visible_sequences": [step.sequence for step in context.steps],
        "omitted_ranges": [span.model_dump(mode="json") for span in context.omitted_ranges],
        "truncated_sequences": [
            step.sequence for step in context.steps if isinstance(step, TruncatedTraceStep)
        ],
    }
