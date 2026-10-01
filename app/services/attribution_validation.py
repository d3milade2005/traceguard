from app.core.exceptions import AttributionValidationError
from app.schemas.attributions import AttributionDecision
from app.schemas.context import AnalysisContext
from app.schemas.traces import CanonicalTrace
from app.services.attribution_input import validate_context_source


def validate_attribution(
    decision: AttributionDecision, context: AnalysisContext, canonical: CanonicalTrace
) -> None:
    """Check visible references without repairing or interpreting uncertain model output."""
    validate_context_source(context, canonical)
    visible = {step.sequence for step in context.steps}
    if decision.decision == "attributed":
        sequence = decision.decisive_sequence
        agents = {step.agent_id for step in context.steps if step.agent_id is not None}
        if decision.responsible_agent not in agents or sequence is None or sequence not in visible:
            raise AttributionValidationError
        if not any(ref.start_sequence <= sequence <= ref.end_sequence for ref in decision.evidence):
            raise AttributionValidationError
    for reference in decision.evidence:
        if reference.end_sequence >= len(canonical.steps):
            raise AttributionValidationError
        # Cardinality avoids iterating over an arbitrarily large provider-supplied range.
        observed = sum(
            reference.start_sequence <= sequence <= reference.end_sequence for sequence in visible
        )
        if observed != reference.end_sequence - reference.start_sequence + 1:
            raise AttributionValidationError
