import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Thread
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from celery.contrib.testing.worker import start_worker
from fastapi.testclient import TestClient
from kombu import Queue
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.exceptions import QueuePublishError, SubmissionConflictError
from app.db.models import Analysis, Run, TraceStep
from app.db.models.auth import AuthSession, User
from app.integrations.task_queue import CeleryRunPublisher
from app.integrations.traces.sources import trace_adapters
from app.main import create_app
from app.schemas.attributions import AttributionDecision, EvidenceReference, LLMResult
from app.services.analysis_factory import create_analysis_service
from app.services.attribution import AttributionService
from app.services.auth import token_digest
from app.services.context import ContextManager
from app.services.normalization import NormalizationService
from app.services.regression import RegressionService
from app.services.runs import RunService
from app.services.submission import SubmissionService
from app.workers.factory import create_celery_app
from examples.langgraph_refund.integration import TraceGuardClient, TraceGuardError

pytestmark = [pytest.mark.integration, pytest.mark.auth]


class Publisher:
    def __init__(self):
        self.calls = []
        self.fail = False

    def publish(self, identifier, request_id):
        self.calls.append(identifier)
        if self.fail:
            raise QueuePublishError(run_id=identifier)


@pytest.fixture
def graph_setup(postgres):
    pytest.importorskip("langgraph")
    from examples.langgraph_refund.application import execute_demo

    _, sessions, _ = postgres
    settings = get_settings()
    recorder, payload, result = execute_demo()
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    runs, publisher = RunService(sessions, settings.max_request_body_bytes), Publisher()
    submission = SubmissionService(runs, publisher, trace_adapters())
    normalization = NormalizationService(
        sessions,
        trace_adapters(),
        lease_seconds=90,
        max_trace_bytes=settings.max_request_body_bytes,
    )
    token = "t" * 43
    with sessions.begin() as session:
        now = datetime.now(UTC)
        user = User(github_id=1234567, username="langgraph-test", last_login_at=now)
        session.add(user)
        session.flush()
        session.add(
            AuthSession(
                token_sha256=token_digest(token),
                user_id=user.id,
                created_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
    with TestClient(create_app(settings)) as client:
        client.app.state.runs = runs
        client.app.state.submission = submission
        client.app.state.run_reads.sessions = sessions
        service = create_analysis_service(sessions, settings, client.app.state.queue)
        service.attribution_publisher = Publisher()
        service.regression_publisher = Publisher()
        service.validation_publisher = Publisher()
        client.app.state.analyses = service
        client.cookies.set(settings.session_cookie_name, token)
        client.headers["Origin"] = settings.frontend_origin
        client.headers["Content-Type"] = "application/json"
        yield client, runs, publisher, normalization, service, payload, raw, result


def test_real_graph_ingestion_normalization_and_dashboard_trace(graph_setup):
    client, runs, publisher, normalize, _, payload, raw, result = graph_setup
    response = client.post("/api/v1/runs?source=langgraph", content=raw)
    assert response.status_code == 202
    run_id = UUID(response.json()["id"])
    assert runs.get(run_id).original_trace == raw
    assert runs.get(run_id).failure_signal == "evaluator_failed"
    assert publisher.calls == [run_id]
    normalize.process(run_id)
    normalize.process(run_id)
    canonical = client.get(f"/api/v1/runs/{run_id}/trace").json()
    assert [s["sequence"] for s in canonical["steps"]] == list(range(5))
    assert canonical["steps"][3]["agent_id"] == "Eligibility Decision Agent"
    assert canonical["steps"][3]["output"] == {"approved": result["approved"]}
    assert canonical["steps"][4]["input"]["approved"] is True
    assert client.get(f"/api/v1/runs/{run_id}/original-trace").content == raw
    assert client.get("/api/v1/runs").json()["items"][0]["id"] == str(run_id)
    assert client.get("/api/v1/dashboard").json()["recent_runs"][0]["id"] == str(run_id)
    assert payload["events"][2]["tool_name"] == canonical["steps"][2]["tool_name"]


def test_concurrent_duplicate_ingestion_reuses_one_run_and_one_task(graph_setup):
    _, runs, publisher, normalize, _, _, raw, _ = graph_setup
    service = SubmissionService(runs, publisher, trace_adapters())
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda _: service.submit("langgraph", raw, "duplicate"), range(4)))
    assert len({r.id for r in records}) == 1
    assert len(publisher.calls) == 1
    normalize.process(records[0].id)
    assert service.submit("langgraph", raw, "duplicate").status == "normalized"
    with runs.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 1
        assert session.scalar(select(func.count()).select_from(TraceStep)) == 5


def test_changed_payload_conflicts_and_failed_publication_is_not_requeued(graph_setup):
    client, runs, publisher, _, _, payload, raw, _ = graph_setup
    publisher.fail = True
    first = client.post("/api/v1/runs?source=langgraph", content=raw)
    assert first.status_code == 503
    run_id = UUID(first.json()["error"]["run_id"])
    assert runs.get(run_id).original_trace == raw
    repeated = client.post("/api/v1/runs?source=langgraph", content=raw)
    assert repeated.status_code == 200 and repeated.json()["status"] == "failed"
    assert len(publisher.calls) == 1
    payload["task"] += " changed"
    conflict = client.post("/api/v1/runs?source=langgraph", json=payload)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == SubmissionConflictError.code


def test_ingestion_requires_existing_session_origin_and_explicit_failure(graph_setup):
    client, runs, _, _, _, payload, raw, _ = graph_setup
    client.cookies.clear()
    assert client.post("/api/v1/runs?source=langgraph", content=raw).status_code == 401
    settings = get_settings()
    client.cookies.set(settings.session_cookie_name, "t" * 43)
    client.headers["Origin"] = "https://untrusted.example"
    assert client.post("/api/v1/runs?source=langgraph", content=raw).status_code == 403
    client.headers["Origin"] = settings.frontend_origin
    del payload["failure"]
    assert client.post("/api/v1/runs?source=langgraph", json=payload).status_code == 422
    with runs.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 0


def diagnosis():
    return LLMResult(
        attribution=AttributionDecision(
            decision="attributed",
            responsible_agent="Eligibility Decision Agent",
            decisive_sequence=3,
            failure_category="refund policy",
            confidence="high",
            abstention_reason=None,
            explanation="The eligibility agent approved 21 elapsed days under a 14-day policy.",
            evidence=(
                EvidenceReference(start_sequence=0, end_sequence=4, reason="Actual graph events"),
            ),
        )
    )


def test_existing_coordinator_stops_external_replay_for_review_without_generation(graph_setup):
    client, runs, _, normalize, service, _, raw, _ = graph_setup
    run_id = UUID(client.post("/api/v1/runs?source=langgraph", content=raw).json()["id"])
    normalize.process(run_id)
    service.attributions.submit(
        run_id, "graph-test", Publisher(), model="test", reasoning_effort="medium"
    )
    fake = Mock()
    fake.attribute.return_value = diagnosis()
    attr_worker = AttributionService(
        runs.sessions, runs, ContextManager(get_settings().analysis_context), fake, lease_seconds=60
    )
    spec_worker = RegressionService(
        runs.sessions,
        runs,
        service.attributions,
        fake,
        lease_seconds=60,
        max_input_bytes=48000,
        max_scenario_bytes=16000,
    )
    client.app.state.queue.send_task = Mock()
    response = client.post(f"/api/v1/runs/{run_id}/analysis")
    assert response.status_code == 202
    for _ in range(8):
        pending = service.advance(run_id)
        record = service.get(run_id)
        if not pending:
            break
        if record.attribution_id:
            attr_worker.process(record.attribution_id)
        if record.regression_spec_id:
            spec_worker.process(record.regression_spec_id)
    assert record.status == "review_required"
    assert record.stop_reason == "regression_needs_review"
    spec = service.regressions.get(record.regression_spec_id)
    assert spec.disposition == "needs_review" and "no approved replay" in spec.review_reason
    assert record.generated_test_id is None and record.validation_id is None
    assert fake.attribute.call_count == 1 and fake.generate.call_count == 0
    repeated = client.post(f"/api/v1/runs/{run_id}/analysis")
    assert repeated.status_code == 200 and repeated.json()["id"] == str(record.id)
    ledger = client.get("/api/v1/dashboard").json()["recent_runs"][0]
    assert ledger["responsible_agent"] == "Eligibility Decision Agent"
    assert ledger["analysis_status"] == "review_required"
    assert ledger["validation_verdict"] is None
    with runs.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Analysis)) == 1


def test_real_http_helper_and_rabbitmq_workers_no_paid_inference(
    graph_setup, integration_settings, monkeypatch
):
    import socket

    import uvicorn

    if integration_settings.test_rabbitmq_url is None:
        pytest.skip("TEST_RABBITMQ_URL required")
    client, runs, _, _, _, payload, _, _ = graph_setup
    settings = get_settings().model_copy(
        update={"rabbitmq_url": integration_settings.test_rabbitmq_url}
    )
    fake = Mock()
    fake.attribute.return_value = diagnosis()
    monkeypatch.setattr("app.workers.attribution_tasks.OpenAILLMClient", lambda *a, **k: fake)
    queue = create_celery_app(settings)
    queue_name = "traceguard_test_" + uuid4().hex
    queue.conf.task_default_queue = queue_name
    queue.conf.task_queues = (Queue(queue_name, auto_delete=True),)
    client.app.state.queue = queue
    client.app.state.submission = SubmissionService(
        runs, CeleryRunPublisher(queue, 3), trace_adapters()
    )
    client.app.state.analyses = create_analysis_service(runs.sessions, settings, queue)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(client.app, lifespan="off", log_level="error"))
    thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        helper = TraceGuardClient(
            base, f"{settings.session_cookie_name}={'t' * 43}", origin=settings.frontend_origin
        )
        with start_worker(
            queue,
            pool="solo",
            concurrency=1,
            queues=[queue_name],
            perform_ping_check=False,
            shutdown_timeout=15,
        ):
            response = helper.submit_failed(payload, auto_analyze=True, wait_timeout=30)
            run_id = response["id"]
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                record = helper.get_analysis(run_id)
                if record["status"] != "active":
                    break
                time.sleep(0.2)
            assert record["status"] == "review_required"
            assert record["stop_reason"] == "regression_needs_review"
            again = helper.submit_failed(payload, auto_analyze=True)
            assert again["id"] == run_id and again["analysis"]["id"] == record["id"]
            assert fake.attribute.call_count == 1
            assert record["validation_id"] is None
        unauthenticated = TraceGuardClient(base, "session=invalid", origin=settings.frontend_origin)
        with pytest.raises(TraceGuardError) as error:
            unauthenticated.submit_failed(payload)
        assert error.value.status == 401
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        queue.close()
