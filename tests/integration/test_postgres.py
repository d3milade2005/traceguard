from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError

from app.db.models import Run, RunStatus, TraceStep
from app.db.session import check_database
from app.schemas.runs import RunCreate
from app.services.runs import RunService

pytestmark = pytest.mark.integration


def create_run(sessions):
    data = RunCreate(
        source="developer",
        failure_signal="reported incorrect result",
        original_trace=b'{ "duplicate": 1, "duplicate": 2 }\r\n',
    )
    return RunService(sessions, 10000).create(data)


def test_original_trace_commits_exactly_and_steps_round_trip(postgres):
    engine, sessions, _ = postgres
    check_database(engine)
    run = create_run(sessions)
    with sessions.begin() as session:
        session.add(
            TraceStep(
                run_id=run.id,
                sequence=0,
                agent_id="planner",
                event_type="tool_call",
                status="error",
                tool_name="search",
                tool_arguments={"query": "example"},
                tool_result={"error": "timeout"},
                occurred_at=datetime.now(UTC),
                step_metadata={"framework_step": 17},
            )
        )
    with sessions() as session:
        persisted = session.get(Run, run.id)
        assert persisted.original_trace == b'{ "duplicate": 1, "duplicate": 2 }\r\n'
        assert persisted.created_at.tzinfo is not None
        step = session.scalars(select(TraceStep).where(TraceStep.run_id == run.id)).one()
        assert step.tool_arguments == {"query": "example"}
        assert step.step_metadata == {"framework_step": 17}
        assert step.occurred_at.tzinfo is not None


def test_constraint_failure_rolls_back_entire_transaction(postgres):
    _, sessions, _ = postgres
    with pytest.raises(IntegrityError), sessions.begin() as session:
        first = Run(
            source="test",
            failure_signal="timeout",
            original_trace=b"{}",
            trace_content_type="application/json",
        )
        session.add(first)
        session.flush()
        session.add(TraceStep(run_id=first.id, sequence=-1, event_type="invalid"))
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 0
    assert create_run(sessions).status == RunStatus.PENDING


@pytest.mark.parametrize(
    "violation", ["duplicate_sequence", "missing_run", "invalid_failure_state"]
)
def test_relational_constraints(postgres, violation):
    _, sessions, _ = postgres
    run = create_run(sessions)
    with pytest.raises(IntegrityError), sessions.begin() as session:
        if violation == "duplicate_sequence":
            session.add_all(
                [
                    TraceStep(run_id=run.id, sequence=0, event_type="message"),
                    TraceStep(run_id=run.id, sequence=0, event_type="message"),
                ]
            )
        elif violation == "missing_run":
            session.add(TraceStep(run_id=uuid4(), sequence=0, event_type="message"))
        else:
            persisted = session.get(Run, run.id)
            persisted.status = RunStatus.FAILED


def test_failure_recording_is_durable_repeatable_and_preserves_source(postgres):
    _, sessions, _ = postgres
    run = create_run(sessions)
    service = RunService(sessions, 10000)
    assert service.record_failure(run.id, "NORMALIZATION_FAILED")
    assert not service.record_failure(run.id, "OTHER_FAILURE")
    failed = service.get(run.id)
    assert failed.status == RunStatus.FAILED
    assert failed.failure_code == "NORMALIZATION_FAILED"
    assert failed.failed_at.tzinfo is not None
    assert failed.original_trace == run.original_trace


def test_migrations_match_metadata_and_round_trip(postgres):
    engine, _, config = postgres
    command.check(config)
    command.downgrade(config, "base")
    assert "runs" not in inspect(engine).get_table_names()
    assert "trace_steps" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    command.check(config)
    assert {"runs", "trace_steps"} <= set(inspect(engine).get_table_names())
