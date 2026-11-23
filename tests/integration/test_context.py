import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.core.run_status import RunStatus
from app.db.models import Run, TraceStep
from app.integrations.traces.sources import trace_adapters
from app.schemas.runs import RunCreate
from app.schemas.traces import TraceStep as CanonicalStep
from app.services.context import ContextManager
from app.services.normalization import NormalizationService
from app.services.runs import RunService

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "who_when"


@pytest.mark.parametrize("variant", ["algorithm_generated", "hand_crafted"])
def test_persisted_trace_to_context_preserves_all_evidence_and_run_state(postgres, variant):
    _, sessions, _ = postgres
    row = json.loads((FIXTURES / f"{variant}_sample.json").read_text(encoding="utf-8"))[0]
    raw = json.dumps({key: row[key] for key in ("question", "history")}).encode("utf-8")
    runs = RunService(sessions, 10 * 1024 * 1024)
    run = runs.create(
        RunCreate(
            source=f"who_when.{variant}", original_trace=raw, failure_signal="developer_report"
        )
    )
    NormalizationService(
        sessions, trace_adapters(), lease_seconds=90, max_trace_bytes=runs.max_trace_bytes
    ).process(run.id)
    trace = runs.canonical_trace(run.id)
    original = trace.model_dump_json()
    context = ContextManager(Settings().analysis_context).build(trace, run_id=run.id)
    assert context.run_id == run.id
    assert context.task == row["question"]
    assert context.source == run.source
    assert context.statistics.estimated_selected_size <= context.statistics.effective_budget
    assert context.statistics.estimated_selected_size == len(
        context.model_dump_json().encode("utf-8")
    )
    assert context.full_trace_used == (variant == "algorithm_generated")
    # Consumer mutations of derived JSON cannot modify canonical or stored evidence.
    for step in context.steps:
        if isinstance(step, CanonicalStep):
            step.step_metadata["role"] = "consumer edit"
    assert trace.model_dump_json() == original
    assert runs.canonical_trace(run.id).model_dump_json() == original
    assert runs.get(run.id).original_trace == raw
    assert runs.get(run.id).status == RunStatus.NORMALIZED
    with sessions() as session:
        assert len(
            session.scalars(select(TraceStep).where(TraceStep.run_id == run.id)).all()
        ) == len(trace.steps)
        persisted = session.get(Run, run.id)
        assert persisted.processing_attempts == 1
        assert persisted.claim_token is None
