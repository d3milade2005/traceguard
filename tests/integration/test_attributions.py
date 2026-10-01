import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from alembic import command
from celery.contrib.testing.worker import start_worker
from fastapi.testclient import TestClient
from kombu import Queue
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from app.core.attribution_status import AttributionStatus
from app.core.config import get_settings
from app.core.context import ContextPolicy
from app.core.exceptions import (
    AttributionQueuePublishError,
    LLMOutputLimitError,
    ProcessingRetryError,
    RunClaimedError,
)
from app.core.run_status import RunStatus
from app.db.models import Attribution, TraceStep
from app.integrations.task_queue import CeleryAttributionPublisher
from app.integrations.traces.sources import trace_adapters
from app.main import create_app
from app.schemas.attributions import ProviderUsage
from app.schemas.runs import RunCreate
from app.services.attribution import MAX_ATTRIBUTION_ATTEMPTS, AttributionService
from app.services.attribution_input import build_attribution_request
from app.services.attributions import AttributionStore
from app.services.context import ContextManager
from app.services.normalization import NormalizationService
from app.services.runs import RunService
from app.workers.factory import create_celery_app
from tests.fakes import FakeLLMClient

pytestmark = pytest.mark.integration
RAW = (
    b'{"question":"Return 4","history":'
    b'[{"name":"A","role":"assistant","content":"The answer is 9"}]}'
)


class Publisher:
    def __init__(self, store, fail=False):
        self.store = store
        self.fail = fail
        self.calls = []

    def publish(self, attribution_id, request_id):
        # This independent session must see the committed pending operation.
        assert self.store.get(attribution_id).status == AttributionStatus.PENDING
        self.calls.append(attribution_id)
        if self.fail:
            raise AttributionQueuePublishError(attribution_id=attribution_id)


@pytest.fixture
def attribution_setup(postgres):
    _, sessions, _ = postgres
    settings = get_settings()
    runs = RunService(sessions, settings.max_request_body_bytes)
    run = runs.create(
        RunCreate(
            source="who_when.algorithm_generated",
            original_trace=RAW,
            failure_signal="developer_report",
        )
    )
    NormalizationService(
        sessions,
        trace_adapters(),
        lease_seconds=90,
        max_trace_bytes=settings.max_request_body_bytes,
    ).process(run.id)
    store = AttributionStore(sessions)
    publisher = Publisher(store)
    fake = FakeLLMClient()
    manager = ContextManager(settings.analysis_context)
    service = AttributionService(sessions, runs, manager, fake, lease_seconds=120)
    return run.id, runs, store, publisher, fake, service


def submit(setup):
    run_id, _, store, publisher, _, _ = setup
    return store.submit(
        run_id, "attribution-test", publisher, model="gpt-5.6-luna", reasoning_effort="medium"
    )[0]


@pytest.mark.parametrize("mode", ["attributed", "abstain"])
def test_fake_result_persists_and_duplicate_delivery_has_no_second_call(attribution_setup, mode):
    run_id, runs, store, _, fake, service = attribution_setup
    fake.mode = mode
    accepted = submit(attribution_setup)
    service.process(accepted.id)
    service.process(accepted.id)
    result = store.get(accepted.id)
    assert result.status == AttributionStatus.COMPLETED
    assert result.decision == mode
    assert len(fake.requests) == 1
    assert result.input_tokens == 100 and result.reasoning_tokens == 20
    assert result.response_id == "resp_fake"
    assert result.context_manifest["visible_sequences"] == [0]
    assert len(result.input_sha256) == 64
    assert result.completed_at is not None
    assert len(result.evidence) == (1 if mode == "attributed" else 0)
    assert runs.get(run_id).status == RunStatus.NORMALIZED
    assert runs.get(run_id).original_trace == RAW


def test_repeated_and_concurrent_post_create_one_operation(attribution_setup):
    run_id, _, store, publisher, _, _ = attribution_setup
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: store.submit(
                    run_id, "parallel", publisher, model="gpt-5.6-luna", reasoning_effort="medium"
                ),
                range(2),
            )
        )
    assert results[0][0].id == results[1][0].id
    assert sorted(created for _, created in results) == [False, True]
    assert len(publisher.calls) == 1


@pytest.mark.parametrize(
    "mode,code",
    [
        ("unknown_agent", "ATTRIBUTION_INVALID"),
        ("invalid_sequence", "ATTRIBUTION_INVALID"),
        ("permanent", "LLM_CONFIGURATION_INVALID"),
    ],
)
def test_permanent_or_invalid_result_records_failure_without_evidence(
    attribution_setup, mode, code
):
    _, _, store, _, fake, service = attribution_setup
    fake.mode = mode
    accepted = submit(attribution_setup)
    service.process(accepted.id)
    service.process(accepted.id)
    result = store.get(accepted.id)
    assert result.status == AttributionStatus.FAILED
    assert result.failure_code == code
    assert result.decision is None and not result.evidence
    assert len(fake.requests) == 1
    if mode != "permanent":
        assert result.input_tokens == 100


def test_transient_provider_retry_and_final_failure_are_bounded(attribution_setup):
    _, _, store, _, fake, service = attribution_setup
    accepted = submit(attribution_setup)
    fake.mode = "transient"
    with pytest.raises(ProcessingRetryError):
        service.process(accepted.id)
    assert store.get(accepted.id).status == AttributionStatus.QUEUED
    with pytest.raises(ProcessingRetryError):
        service.process(accepted.id, final_attempt=True)
    assert store.get(accepted.id).status == AttributionStatus.FAILED
    assert store.get(accepted.id).failure_code == "LLM_TEMPORARILY_UNAVAILABLE"


def test_transient_then_success_preserves_operation_identity(attribution_setup):
    _, _, store, _, fake, service = attribution_setup
    accepted = submit(attribution_setup)
    fake.mode = "transient"
    with pytest.raises(ProcessingRetryError):
        service.process(accepted.id)
    fake.mode = "attributed"
    service.process(accepted.id)
    assert store.get(accepted.id).status == AttributionStatus.COMPLETED
    assert len(fake.requests) == 2
    assert fake.requests[0] == fake.requests[1]


def test_worker_ownership_and_expired_claims_fence_stale_results(attribution_setup):
    _, runs, store, _, fake, service = attribution_setup
    accepted = submit(attribution_setup)
    old = service.claim(accepted.id)
    with pytest.raises(RunClaimedError):
        service.claim(accepted.id)
    with runs.sessions.begin() as session:
        session.get(Attribution, accepted.id).claim_expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
    owner = service.claim(accepted.id)
    canonical = runs.canonical_trace(owner.run_id)
    context = service.context_manager.build(canonical)
    result = fake.attribute(
        build_attribution_request(
            context, canonical, model=owner.model, reasoning_effort=owner.reasoning_effort
        )
    )
    assert not service.complete(old, result, context, canonical)
    assert not service.record_usage(old, result.usage)
    assert service.complete(owner, result, context, canonical)
    assert not service.complete(old, result, context, canonical)
    assert len(store.get(accepted.id).evidence) == 1


def test_concurrent_delivery_cannot_make_a_second_inference(attribution_setup):
    _, _, store, _, fake, service = attribution_setup
    accepted = submit(attribution_setup)
    entered, release = Event(), Event()

    class BlockingClient:
        def attribute(self, request):
            entered.set()
            assert release.wait(10)
            return fake.attribute(request)

    service.llm_client = BlockingClient()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.process, accepted.id)
        assert entered.wait(10)
        try:
            with pytest.raises(RunClaimedError):
                service.process(accepted.id)
        finally:
            release.set()
        first.result(timeout=10)
    assert store.get(accepted.id).status == AttributionStatus.COMPLETED
    assert len(fake.requests) == 1


def test_crash_redelivery_attempt_count_is_durable_and_bounded(attribution_setup):
    _, runs, store, _, fake, service = attribution_setup
    accepted = submit(attribution_setup)
    for _ in range(MAX_ATTRIBUTION_ATTEMPTS):
        assert service.claim(accepted.id) is not None
        with runs.sessions.begin() as session:
            session.get(Attribution, accepted.id).claim_expires_at = datetime.now(UTC) - timedelta(
                seconds=1
            )
    service.process(accepted.id)
    assert store.get(accepted.id).failure_code == "ATTRIBUTION_ATTEMPTS_EXHAUSTED"
    assert not fake.requests


def test_result_and_evidence_roll_back_atomically(attribution_setup, postgres):
    _, _, store, _, _, service = attribution_setup
    accepted = submit(attribution_setup)
    engine, _, _ = postgres

    def reject_evidence(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO attribution_evidence"):
            raise IntegrityError("private", {}, Exception("private"))

    event.listen(engine, "after_cursor_execute", reject_evidence)
    try:
        with pytest.raises(IntegrityError):
            service.process(accepted.id)
    finally:
        event.remove(engine, "after_cursor_execute", reject_evidence)
    result = store.get(accepted.id)
    assert result.status == AttributionStatus.FAILED
    assert result.decision is None and not result.evidence


def test_incomplete_output_usage_persists_without_a_decision(attribution_setup):
    _, _, store, _, _, service = attribution_setup
    accepted = submit(attribution_setup)

    class IncompleteClient:
        def attribute(self, request):
            raise LLMOutputLimitError(
                usage=ProviderUsage(response_id="resp_limit", input_tokens=100, output_tokens=4096)
            )

    service.llm_client = IncompleteClient()
    service.process(accepted.id)
    result = store.get(accepted.id)
    assert result.failure_code == "LLM_OUTPUT_LIMIT"
    assert result.output_tokens == 4096 and result.response_id == "resp_limit"
    assert result.decision is None and not result.evidence


def test_truncated_evidence_is_marked_and_canonical_content_remains_unchanged(
    attribution_setup,
):
    run_id, runs, store, _, fake, _ = attribution_setup
    # A small budget forces the committed canonical step into an explicit excerpt.
    service = AttributionService(
        runs.sessions,
        runs,
        ContextManager(ContextPolicy(budget=1600, reserved_headroom=0)),
        fake,
        lease_seconds=120,
    )
    with runs.sessions.begin() as session:
        step = session.scalars(select(TraceStep).where(TraceStep.run_id == run_id)).one()
        step.output = "large-evidence" * 1000
    original = runs.canonical_trace(run_id)
    accepted = submit(attribution_setup)
    service.process(accepted.id)
    result = store.get(accepted.id)
    assert result.status == "completed"
    assert result.evidence[0].contains_truncated
    assert result.context_manifest["truncated_sequences"] == [0]
    assert runs.canonical_trace(run_id) == original
    assert runs.get(run_id).original_trace == RAW


def test_api_trigger_get_repeated_post_and_errors(attribution_setup):
    run_id, runs, store, publisher, _, service = attribution_setup
    with TestClient(create_app(get_settings())) as client:
        client.app.state.attributions = store
        client.app.state.attribution_publisher = publisher
        response = client.post(f"/api/v1/runs/{run_id}/attributions")
        assert response.status_code == 202
        attribution_id = publisher.calls[0]
        assert response.json() == {
            "id": str(attribution_id),
            "run_id": str(run_id),
            "status": "queued",
        }
        assert client.post(f"/api/v1/runs/{run_id}/attributions").status_code == 200
        service.process(attribution_id)
        result = client.get(response.headers["location"])
        assert result.status_code == 200
        assert result.json()["decisive_sequence"] == 0
        assert result.json()["evidence"][0]["start_sequence"] == 0
        for forbidden in (
            "claim_token",
            "original_trace",
            "api_key",
            "instructions",
            "evidence_json",
        ):
            assert forbidden not in result.json()
        assert (
            client.get(f"/api/v1/attributions/{uuid4()}").json()["error"]["code"]
            == "ATTRIBUTION_NOT_FOUND"
        )
        assert client.post(f"/api/v1/runs/{uuid4()}/attributions").status_code == 404
        pending = runs.create(
            RunCreate(source="custom", original_trace=b"{}", failure_signal="developer_report")
        )
        assert client.post(f"/api/v1/runs/{pending.id}/attributions").status_code == 409
    assert len(publisher.calls) == 1


def test_queue_failure_is_durable_and_api_does_not_return_false_acceptance(attribution_setup):
    run_id, _, store, publisher, fake, service = attribution_setup
    publisher.fail = True
    with TestClient(create_app(get_settings())) as client:
        client.app.state.attributions = store
        client.app.state.attribution_publisher = publisher
        response = client.post(f"/api/v1/runs/{run_id}/attributions")
    assert response.status_code == 503
    assert response.json()["error"]["attribution_id"] == str(publisher.calls[0])
    result = store.get(publisher.calls[0])
    assert result.failure_code == "QUEUE_PUBLICATION_FAILED"
    service.process(result.id)
    assert not fake.requests


def test_migration_schema_constraints_and_downgrade_preserve_canonical_evidence(
    attribution_setup, postgres
):
    run_id, runs, _, _, _, _ = attribution_setup
    _, sessions, config = postgres
    command.check(config)
    accepted = submit(attribution_setup)
    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.add(
            Attribution(
                run_id=run_id,
                provider="openai",
                model="model",
                reasoning_effort="medium",
                prompt_version="attribution-v1",
            )
        )
    with pytest.raises(IntegrityError), sessions.begin() as session:
        row = session.get(Attribution, accepted.id)
        row.status = "completed"
        row.completed_at = datetime.now(UTC)
    command.downgrade(config, "0002")
    assert runs.get(run_id).original_trace == RAW
    assert len(runs.canonical_trace(run_id).steps) == 1
    command.upgrade(config, "head")
    command.check(config)


def test_real_api_and_celery_rabbitmq_worker_with_fake_llm(
    attribution_setup, integration_settings, monkeypatch
):
    if integration_settings.test_rabbitmq_url is None:
        pytest.skip("Set TEST_RABBITMQ_URL for the attribution worker smoke test")
    run_id, _, store, _, fake, _ = attribution_setup
    settings = get_settings().model_copy(
        update={"rabbitmq_url": integration_settings.test_rabbitmq_url}
    )
    monkeypatch.setattr(
        "app.workers.attribution_tasks.OpenAILLMClient", lambda *args, **kwargs: fake
    )
    app = create_celery_app(settings)
    queue = "traceguard_attribution_test_" + uuid4().hex
    app.conf.task_default_queue = queue
    app.conf.task_queues = (Queue(queue, auto_delete=True),)
    try:
        with (
            start_worker(
                app,
                pool="solo",
                concurrency=1,
                queues=[queue],
                perform_ping_check=False,
                shutdown_timeout=30,
            ),
            TestClient(create_app(settings)) as client,
        ):
            client.app.state.attributions = store
            client.app.state.attribution_publisher = CeleryAttributionPublisher(app, 3)
            response = client.post(f"/api/v1/runs/{run_id}/attributions")
            assert response.status_code == 202
            location = response.headers["location"]
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                result = client.get(location).json()
                if result["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.05)
            assert result["status"] == "completed"
            assert len(fake.requests) == 1
    finally:
        app.close()
