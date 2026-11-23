from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.context import ContextPolicy
from app.core.exceptions import RunNotFoundError, RunNotNormalizedError
from app.core.run_status import RunStatus
from app.db.models import TraceStep as PersistedStep
from app.schemas.traces import TraceStep
from app.services.context import ContextManager
from app.services.runs import RunService


def loader(row, steps=()):
    session = Mock()
    session.execute.return_value.one_or_none.return_value = row
    session.scalars.return_value.all.return_value = steps
    sessions = Mock()
    sessions.return_value.__enter__ = Mock(return_value=session)
    sessions.return_value.__exit__ = Mock(return_value=False)
    return RunService(sessions, 10000), session


def test_persisted_canonical_loading_and_selection_never_modify_orm_payloads():
    step = TraceStep(
        sequence=0,
        event_type="tool_result",
        output={"items": ["huge" * 10000]},
        tool_result={"nested": [1, 2]},
        step_metadata={"role": "human"},
    )
    persisted = PersistedStep(run_id=uuid4(), **step.model_dump())
    before = deepcopy({name: getattr(persisted, name) for name in TraceStep.model_fields})
    service, session = loader(
        SimpleNamespace(source="framework", task="Task", status=RunStatus.NORMALIZED),
        [persisted],
    )
    trace = service.canonical_trace(persisted.run_id)
    context = ContextManager(ContextPolicy(budget=2000, reserved_headroom=0)).build(trace)
    assert context.steps[0].sequence == 0
    trace.steps[0].step_metadata["role"] = "consumer edit"
    trace.steps[0].tool_result["nested"].append(3)
    trace.steps[0].output["items"].append("consumer edit")
    assert {name: getattr(persisted, name) for name in TraceStep.model_fields} == before
    session.commit.assert_not_called()
    session.add.assert_not_called()
    session.flush.assert_not_called()


def test_unknown_run_has_no_canonical_trace():
    service, session = loader(None)
    with pytest.raises(RunNotFoundError):
        service.canonical_trace(uuid4())
    session.scalars.assert_not_called()


@pytest.mark.parametrize("status", [s for s in RunStatus if s != RunStatus.NORMALIZED])
def test_incomplete_run_cannot_be_selected(status):
    service, session = loader(SimpleNamespace(source="custom", task=None, status=status))
    with pytest.raises(RunNotNormalizedError):
        service.canonical_trace(uuid4())
    session.scalars.assert_not_called()


@pytest.mark.parametrize("sequences", [[], [1], [0, 2]])
def test_corrupt_or_incomplete_persisted_sequences_are_not_silently_repaired(sequences):
    service, _ = loader(
        SimpleNamespace(source="custom", task="Task", status=RunStatus.NORMALIZED),
        [
            PersistedStep(**TraceStep(sequence=i, event_type="message").model_dump())
            for i in sequences
        ],
    )
    with pytest.raises(ValidationError):
        service.canonical_trace(uuid4())
