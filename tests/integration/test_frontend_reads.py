from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.core.config import get_settings
from app.db.models import Analysis, Run
from app.main import create_app
from app.schemas.runs import RunListQuery
from app.services.run_reads import RunReadService
from tests.integration.test_generated_tests import compilation_setup as compilation_setup
from tests.integration.test_validations import validation_setup as validation_setup

pytestmark = pytest.mark.integration


def add_run(session, *, failed=False, created=None):
    row = Run(
        source="controlled.calculator",
        original_trace=b"PRIVATE_LARGE_TRACE",
        failure_signal="developer_report",
        trace_content_type="application/json",
        status="failed" if failed else "normalized",
        task="Task",
        created_at=created or datetime.now(UTC),
    )
    if failed:
        row.failure_code = "NORMALIZATION_FAILED"
        row.failed_at = datetime.now(UTC)
    session.add(row)
    session.flush()
    return row.id


def test_empty_ledger_dashboard_and_pagination_filters_are_consistent(postgres):
    _, sessions, _ = postgres
    service = RunReadService(sessions)
    assert service.list(RunListQuery()).total == 0
    assert service.dashboard().total_runs == 0
    now = datetime.now(UTC)
    with sessions.begin() as session:
        identifiers = [add_run(session, failed=i == 1, created=now) for i in range(4)]
        session.add(
            Analysis(
                run_id=identifiers[2],
                model="fake",
                reasoning_effort="medium",
                request_id="active",
                deadline_at=now + timedelta(hours=1),
            )
        )
        session.add(
            Analysis(
                run_id=identifiers[3],
                model="fake",
                reasoning_effort="medium",
                request_id="expired",
                deadline_at=now - timedelta(seconds=1),
            )
        )
    first = service.list(RunListQuery(limit=2))
    second = service.list(RunListQuery(limit=2, offset=2))
    assert first.total == second.total == 4
    assert [r.id for r in first.items + second.items] == sorted(identifiers, reverse=True)
    assert service.list(RunListQuery(status="failed")).total == 1
    assert service.list(RunListQuery(analysis_status="active")).total == 1
    expired = service.list(RunListQuery(analysis_status="failed"))
    assert (
        expired.total == 1 and expired.items[0].analysis_stop_reason == "ANALYSIS_DEADLINE_EXCEEDED"
    )
    assert service.list(RunListQuery(needs_attention=True)).total == 2
    dashboard = service.dashboard()
    assert dashboard.total_runs == 4 and dashboard.active_analyses == 1
    assert len(dashboard.needs_attention_runs) == 2
    # The projection reports expired work without mutating durable analysis records.
    with sessions() as session:
        assert session.get(Analysis, expired.items[0].analysis_id).status == "active"


def test_real_stage_ids_valid_verdict_and_lightweight_sql(postgres, validation_setup):
    engine, sessions, _ = postgres
    artifact, validation, publisher, _ = validation_setup
    accepted, _ = validation.submit(artifact.id, "read-test", publisher)
    validation.process(accepted.id)
    statements = []

    def captured(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", captured)
    try:
        service = RunReadService(sessions)
        ledger = service.list(
            RunListQuery(validation_verdict="valid", responsible_agent="calculator")
        )
        dashboard = service.dashboard()
        detail = service.summary(ledger.items[0].id)
    finally:
        event.remove(engine, "before_cursor_execute", captured)
    assert ledger.total == dashboard.valid_regressions == 1
    assert detail.validation_id == accepted.id and detail.generated_test_id == artifact.id
    assert detail.attribution_id and detail.regression_spec_id and detail.normalized_step_count == 2
    for statement in statements:
        assert (
            "runs.original_trace" not in statement
            and "generated_tests.source_code" not in statement
        )
    with TestClient(create_app(get_settings())) as client:
        client.app.state.run_reads = service
        body = client.get("/api/v1/runs").json()
        assert body["total"] == 1 and body["items"][0]["validation_verdict"] == "valid"
        assert "source_code" not in str(body) and "original_trace" not in str(body)
        assert client.get(f"/api/v1/runs/{detail.id}").json()["validation_id"] == str(accepted.id)
