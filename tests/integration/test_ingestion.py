import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.config import get_settings
from app.core.exceptions import (
    ProcessingRetryError,
    QueuePublishError,
    RunClaimedError,
    TraceAdapterError,
)
from app.core.run_status import RunStatus
from app.db.models import Run, TraceStep
from app.integrations.traces.contracts import decode_record
from app.integrations.traces.sources import trace_adapters
from app.main import create_app
from app.schemas.runs import RunCreate
from app.schemas.traces import CanonicalTrace
from app.services.normalization import MAX_PROCESSING_ATTEMPTS, NormalizationService
from app.services.runs import RunService
from app.services.submission import SubmissionService

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "who_when"
SOURCE = "who_when.algorithm_generated"
RAW = (
    b' { "history" : [{"content":"","role":"assistant","name":"Agent"}],\r\n "question": "Why?" }\n'
)


class Publisher:
    def __init__(self, runs, *, fail=False):
        self.runs = runs
        self.fail = fail
        self.calls = []

    def publish(self, run_id, request_id):
        # A DIFFERENT session must see the committed raw evidence before publication.
        persisted = self.runs.get(run_id)
        assert persisted.status == RunStatus.PENDING
        self.calls.append((run_id, request_id, persisted.original_trace))
        if self.fail:
            raise QueuePublishError(run_id=run_id)


@pytest.fixture
def services(postgres):
    _, sessions, _ = postgres
    runs = RunService(sessions, 10 * 1024 * 1024)
    publisher = Publisher(runs)
    submission = SubmissionService(runs, publisher, trace_adapters())
    normalization = NormalizationService(
        sessions, trace_adapters(), lease_seconds=90, max_trace_bytes=runs.max_trace_bytes
    )
    return runs, publisher, submission, normalization


def stored_run(runs, raw=RAW, source=SOURCE):
    return runs.create(
        RunCreate(source=source, original_trace=raw, failure_signal="developer_report")
    )


def expire_claim(runs, run_id):
    with runs.sessions.begin() as session:
        run = session.get(Run, run_id)
        run.claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)


def steps(runs, run_id):
    with runs.sessions() as session:
        return session.scalars(
            select(TraceStep).where(TraceStep.run_id == run_id).order_by(TraceStep.sequence)
        ).all()


def test_api_submission_exact_bytes_and_retrieval(services):
    runs, publisher, submission, normalization = services
    with TestClient(create_app(get_settings())) as client:
        client.app.state.submission = submission
        response = client.post(
            f"/api/v1/runs?source={SOURCE}",
            content=RAW,
            headers={"Content-Type": "application/json", "X-Request-ID": "ingest-request"},
        )
        assert response.status_code == 202
        run_id = response.json()["id"]
        assert response.json() == {"id": run_id, "status": "queued"}
        assert response.headers["location"].endswith(f"/api/v1/runs/{run_id}")
        assert publisher.calls[0][1:] == ("ingest-request", RAW)
        result = client.get(response.headers["location"])
        assert result.status_code == 200
        assert result.json()["source"] == SOURCE
        assert result.json()["status"] == "queued"
        assert result.json()["normalized_step_count"] == 0
        assert "original_trace" not in result.json()
        normalization.process(publisher.calls[0][0])
        result = client.get(response.headers["location"]).json()
        assert result["status"] == "normalized"
        assert result["normalized_step_count"] == 1
        assert result["updated_at"] >= result["created_at"]
        assert result["failure_code"] is None
        missing = client.get(f"/api/v1/runs/{uuid4()}")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "RUN_NOT_FOUND"
    persisted = runs.get(publisher.calls[0][0])
    assert persisted.original_trace == RAW
    assert steps(runs, persisted.id)[0].output == ""


def test_queue_failure_retains_evidence_and_never_returns_202(services):
    runs, publisher, submission, _ = services
    publisher.fail = True
    with TestClient(create_app(get_settings())) as client:
        client.app.state.submission = submission
        response = client.post(
            f"/api/v1/runs?source={SOURCE}",
            content=RAW,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 503
        error = response.json()["error"]
        assert error["code"] == "QUEUE_PUBLICATION_FAILED"
        assert error["run_id"] == str(publisher.calls[0][0])
        result = client.get(f"/api/v1/runs/{error['run_id']}").json()
        assert result["status"] == "failed"
        assert result["failure_code"] == error["code"]
    assert runs.get(publisher.calls[0][0]).original_trace == RAW


@pytest.mark.parametrize("variant", ["algorithm_generated", "hand_crafted"])
def test_fixture_normalization_and_duplicate_delivery(services, variant):
    runs, _, submission, normalization = services
    records = json.loads((FIXTURES / f"{variant}_sample.json").read_text(encoding="utf-8"))
    # Fixtures contain offline labels; the production request deliberately excludes them.
    for record in records:
        raw = json.dumps({key: record[key] for key in ("question", "history")}).encode()
        result = submission.submit(f"who_when.{variant}", raw, "fixture-request")
        expected = trace_adapters()[f"who_when.{variant}"].adapt(decode_record(raw))
        normalization.process(result.id)
        normalization.process(result.id)
        actual = steps(runs, result.id)
        assert len(actual) == len(expected.steps)
        for step, canonical in zip(actual, expected.steps, strict=True):
            assert step.sequence == canonical.sequence
            assert step.agent_id == canonical.agent_id
            assert step.event_type == canonical.event_type
            assert step.output == canonical.output
            assert step.step_metadata == canonical.step_metadata
            assert step.input is None
        assert runs.get(result.id).original_trace == raw
        assert runs.get(result.id).status == RunStatus.NORMALIZED
        with runs.sessions() as session:
            persisted = session.get(Run, result.id)
            assert persisted.task == expected.task
            assert persisted.processing_attempts == 1


def test_concurrent_delivery_waits_for_owner(services):
    runs, _, submission, normalization = services
    result = submission.submit(SOURCE, RAW, "concurrent")
    entered, release = Event(), Event()
    real = trace_adapters()[SOURCE]

    class BlockingAdapter:
        validate_submission = real.validate_submission

        def adapt(self, record):
            entered.set()
            assert release.wait(5)
            return real.adapt(record)

    normalization.adapters = {SOURCE: BlockingAdapter()}
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(normalization.process, result.id)
        assert entered.wait(5)
        try:
            with pytest.raises(RunClaimedError):
                normalization.process(result.id)
        finally:
            release.set()
        first.result(timeout=5)
    normalization.process(result.id)
    assert len(steps(runs, result.id)) == 1


def test_expired_claim_recovery_fences_old_worker(services):
    runs, _, _, normalization = services
    record = stored_run(runs)
    old = normalization.claim(record.id)
    expire_claim(runs, record.id)
    replacement = normalization.claim(record.id)
    canonical = trace_adapters()[SOURCE].adapt(decode_record(RAW))
    assert not normalization.complete(old, canonical)
    assert normalization.complete(replacement, canonical)
    assert not normalization.complete(old, canonical)
    assert len(steps(runs, record.id)) == 1


def test_repeated_worker_crashes_are_bounded(services):
    runs, _, _, normalization = services
    record = stored_run(runs)
    for _ in range(MAX_PROCESSING_ATTEMPTS):
        assert normalization.claim(record.id) is not None
        expire_claim(runs, record.id)
    normalization.process(record.id)
    assert runs.get(record.id).failure_code == "PROCESSING_ATTEMPTS_EXHAUSTED"
    assert runs.get(record.id).original_trace == RAW


@pytest.mark.parametrize(
    ("raw", "source", "code"),
    [
        (b"broken", SOURCE, "TRACE_ADAPTER_ERROR"),
        (b'{"question":"Q","history":[]}', SOURCE, "TRACE_ADAPTER_ERROR"),
        (RAW, "unknown", "UNSUPPORTED_TRACE_SOURCE"),
    ],
)
def test_permanent_input_failures(services, raw, source, code):
    runs, _, _, normalization = services
    record = stored_run(runs, raw, source)
    normalization.process(record.id)
    assert runs.get(record.id).failure_code == code
    assert runs.get(record.id).original_trace == raw
    assert not steps(runs, record.id)
    normalization.process(record.id)  # A terminal failure does not restart.
    with runs.sessions() as session:
        assert session.get(Run, record.id).processing_attempts == 1


@pytest.mark.parametrize("invalid_canonical", [False, True])
def test_adapter_and_canonical_failure(services, invalid_canonical):
    runs, _, _, normalization = services

    class BadAdapter:
        def validate_submission(self, record):
            pass

        def adapt(self, record):
            if invalid_canonical:
                return CanonicalTrace.model_construct(source="fake", task="Q", steps=())
            raise TraceAdapterError("secret trace")

    normalization.adapters = {SOURCE: BadAdapter()}
    record = stored_run(runs)
    normalization.process(record.id)
    persisted = runs.get(record.id)
    assert persisted.failure_code == (
        "INVALID_CANONICAL_TRACE" if invalid_canonical else "TRACE_ADAPTER_ERROR"
    )
    assert persisted.original_trace == RAW
    assert not steps(runs, record.id)


@pytest.mark.parametrize("transient", [False, True])
def test_partial_write_rolls_back_and_transient_retry_succeeds(services, postgres, transient):
    runs, _, _, normalization = services
    raw = b'{"question":"Q","history":[{"role":"A","content":"first"},{"role":"B","content":"second"}]}'
    record = stored_run(runs, raw, "who_when.hand_crafted")
    engine, _, _ = postgres

    def fail_after_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO trace_steps"):
            if transient:
                raise OperationalError("hidden", {}, Exception("private"))
            raise IntegrityError("hidden", {}, Exception("private"))

    event.listen(engine, "after_cursor_execute", fail_after_insert)
    try:
        with pytest.raises(ProcessingRetryError if transient else IntegrityError):
            normalization.process(record.id)
    finally:
        event.remove(engine, "after_cursor_execute", fail_after_insert)
    assert not steps(runs, record.id)
    assert runs.get(record.id).original_trace == raw
    assert runs.get(record.id).status == (RunStatus.QUEUED if transient else RunStatus.FAILED)
    if transient:
        normalization.process(record.id)
        assert len(steps(runs, record.id)) == 2
        assert runs.get(record.id).status == RunStatus.NORMALIZED


def test_final_transient_failure_persisted(services, monkeypatch):
    runs, _, _, normalization = services
    record = stored_run(runs)

    def unavailable(*args):
        raise OperationalError("hidden", {}, Exception("private"))

    monkeypatch.setattr(normalization, "complete", unavailable)
    with pytest.raises(ProcessingRetryError):
        normalization.process(record.id, final_attempt=True)
    assert runs.get(record.id).failure_code == "PROCESSING_UNAVAILABLE"
    assert not steps(runs, record.id)


def test_schema_downgrade_preserves_raw_evidence(services, postgres):
    runs, _, submission, normalization = services
    result = submission.submit(SOURCE, RAW, "downgrade")
    normalization.process(result.id)
    engine, _, config = postgres
    command.check(config)
    command.downgrade(config, "0001")
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT original_trace, status, failure_code FROM runs")
        ).one()
        assert row == (RAW, "failed", "SCHEMA_DOWNGRADED")
        assert connection.scalar(text("SELECT count(*) FROM trace_steps")) == 1
    command.upgrade(config, "head")
    command.check(config)
    assert runs.get(result.id).original_trace == RAW
