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

from app.core.config import get_settings
from app.core.exceptions import (
    LLMConfigurationError,
    LLMOutputLimitError,
    LLMTransientError,
    ProcessingRetryError,
    RegressionQueuePublishError,
    RunClaimedError,
)
from app.db.models import Attribution, RegressionSpecOperation, TraceStep
from app.integrations.task_queue import CeleryRegressionPublisher
from app.integrations.traces.sources import trace_adapters
from app.main import create_app
from app.schemas.attributions import ProviderUsage
from app.schemas.regression_specs import RegressionResult
from app.schemas.runs import RunCreate
from app.services.attribution import AttributionService
from app.services.attributions import AttributionStore
from app.services.context import ContextManager
from app.services.normalization import NormalizationService
from app.services.regression import MAX_REGRESSION_ATTEMPTS, RegressionService
from app.services.regression_input import load_evidence
from app.services.regression_specs import RegressionStore
from app.services.runs import RunService
from app.workers.factory import create_celery_app
from tests.fakes import FakeLLMClient
from tests.unit.test_regression_specs import spec

pytestmark = pytest.mark.integration


class Publisher:
    def __init__(self, store):
        self.store = store
        self.calls = []
        self.fail = False

    def publish(self, operation_id, request_id):
        assert self.store.get(operation_id).status == "pending"
        self.calls.append(operation_id)
        if self.fail:
            raise RegressionQueuePublishError(regression_spec_id=operation_id)


class FakeGenerator:
    def __init__(self):
        self.requests = []
        self.mode = "valid"

    def generate(self, request):
        self.requests.append(request)
        if self.mode == "transient":
            raise LLMTransientError
        if self.mode == "permanent":
            raise LLMConfigurationError
        if self.mode == "incomplete":
            raise LLMOutputLimitError(usage=ProviderUsage(input_tokens=20, output_tokens=4096))
        result = spec()
        if self.mode == "invalid":
            result = result.model_copy(update={"target_agent": "fabricated"})
        elif self.mode == "review":
            result = result.model_copy(
                update={
                    "disposition": "needs_review",
                    "scenario": None,
                    "assertions": (),
                    "review_reason": "Expected behavior is unclear.",
                }
            )
        return RegressionResult(
            spec=result,
            usage=ProviderUsage(
                response_id="resp_regression_fake",
                input_tokens=100,
                output_tokens=200,
                reasoning_tokens=20,
                latency_ms=10,
            ),
        )


@pytest.fixture
def setup_regression(postgres):
    _, sessions, _ = postgres
    settings = get_settings()
    runs = RunService(sessions, settings.max_request_body_bytes)
    run = runs.create(
        RunCreate(
            source="who_when.algorithm_generated",
            original_trace=(
                b'{"question":"Return 4","history":[{"name":"A","role":"assistant","content":"9"}]}'
            ),
            failure_signal="developer_report",
        )
    )
    NormalizationService(
        sessions,
        trace_adapters(),
        lease_seconds=90,
        max_trace_bytes=settings.max_request_body_bytes,
    ).process(run.id)
    with sessions.begin() as session:
        step = session.scalar(select(TraceStep).where(TraceStep.run_id == run.id))
        step.input = "Return 4"
    attributions = AttributionStore(sessions)

    class AttributionPublisher:
        def publish(self, operation_id, request_id):
            assert attributions.get(operation_id).status == "pending"

    attributed, _ = attributions.submit(
        run.id, "test", AttributionPublisher(), model="gpt-5.6-luna", reasoning_effort="medium"
    )
    AttributionService(
        sessions,
        runs,
        ContextManager(settings.analysis_context),
        FakeLLMClient(),
        lease_seconds=120,
    ).process(attributed.id)
    assert attributions.get(attributed.id).status == "completed"
    store = RegressionStore(sessions)
    fake = FakeGenerator()
    publisher = Publisher(store)
    service = RegressionService(
        sessions,
        runs,
        attributions,
        fake,
        lease_seconds=120,
        max_input_bytes=48000,
        max_scenario_bytes=16000,
    )
    return attributed.id, store, publisher, fake, service


def submit(setup):
    attr_id, store, publisher, _, _ = setup
    return store.submit(
        attr_id, "regression-test", publisher, model="gpt-5.6-luna", reasoning_effort="medium"
    )[0]


@pytest.mark.parametrize("mode", ["valid", "review"])
def test_result_and_usage_persist_and_terminal_delivery_is_noop(setup_regression, mode):
    _, store, _, fake, service = setup_regression
    fake.mode = mode
    accepted = submit(setup_regression)
    service.process(accepted.id)
    service.process(accepted.id)
    result = store.get(accepted.id)
    assert result.status == "completed"
    assert result.disposition == ("synthesizable" if mode == "valid" else "needs_review")
    assert result.spec.assertions == (spec().assertions if mode == "valid" else ())
    assert (result.scenario_snapshot is not None) == (mode == "valid")
    assert result.input_tokens == 100 and result.reasoning_tokens == 20
    assert result.response_id == "resp_regression_fake"
    assert result.completed_at is not None and len(result.input_sha256) == 64
    assert len(fake.requests) == 1


def test_repeated_concurrent_submission_publishes_once(setup_regression):
    attr_id, store, publisher, _, _ = setup_regression
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: store.submit(
                    attr_id, "test", publisher, model="gpt-5.6-luna", reasoning_effort="medium"
                ),
                range(2),
            )
        )
    assert results[0][0].id == results[1][0].id
    assert sorted(created for _, created in results) == [False, True]
    assert len(publisher.calls) == 1


@pytest.mark.parametrize("reason", ["abstain", "missing_input", "truncated"])
def test_unreconstructable_evidence_terminates_without_paid_generation(setup_regression, reason):
    attr_id, store, _, fake, service = setup_regression
    from app.db.models import AttributionEvidence

    with service.sessions.begin() as session:
        attr = session.get(Attribution, attr_id)
        if reason == "abstain":
            attr.decision = "abstain"
            attr.responsible_agent = None
            attr.decisive_sequence = None
            attr.failure_category = None
            attr.abstention_reason = "Insufficient evidence"
        elif reason == "truncated":
            session.scalar(
                select(AttributionEvidence).where(AttributionEvidence.attribution_id == attr_id)
            ).contains_truncated = True
        else:
            session.scalar(select(TraceStep).where(TraceStep.run_id == attr.run_id)).input = None
    accepted = submit(setup_regression)
    service.process(accepted.id)
    result = store.get(accepted.id)
    assert result.status == "completed" and result.disposition == "needs_review"
    assert not result.spec.assertions and result.scenario_snapshot is None
    assert result.input_tokens is None and not fake.requests


@pytest.mark.parametrize(
    "mode,code",
    [
        ("invalid", "REGRESSION_SPEC_INVALID"),
        ("permanent", "LLM_CONFIGURATION_INVALID"),
        ("incomplete", "LLM_OUTPUT_LIMIT"),
    ],
)
def test_permanent_and_semantic_failures_do_not_persist_partial_ir(setup_regression, mode, code):
    _, store, _, fake, service = setup_regression
    fake.mode = mode
    accepted = submit(setup_regression)
    service.process(accepted.id)
    service.process(accepted.id)
    result = store.get(accepted.id)
    assert result.status == "failed" and result.failure_code == code
    assert result.spec is None and result.scenario_snapshot is None
    assert len(fake.requests) == 1
    if mode != "permanent":
        assert result.input_tokens is not None


def test_transient_retry_and_final_failure(setup_regression):
    _, store, _, fake, service = setup_regression
    accepted = submit(setup_regression)
    fake.mode = "transient"
    with pytest.raises(ProcessingRetryError):
        service.process(accepted.id)
    assert store.get(accepted.id).status == "queued"
    with pytest.raises(ProcessingRetryError):
        service.process(accepted.id, final_attempt=True)
    assert store.get(accepted.id).status == "failed"
    assert store.get(accepted.id).failure_code == "LLM_TEMPORARILY_UNAVAILABLE"


def test_expired_owner_cannot_write_usage_or_result(setup_regression):
    attr_id, store, _, fake, service = setup_regression
    accepted = submit(setup_regression)
    old = service.claim(accepted.id)
    with pytest.raises(RunClaimedError):
        service.claim(accepted.id)
    with service.sessions.begin() as session:
        session.get(RegressionSpecOperation, accepted.id).claim_expires_at = datetime.now(
            UTC
        ) - timedelta(seconds=1)
    new = service.claim(accepted.id)
    attr = service.attributions.get(attr_id)
    evidence = load_evidence(attr, service.runs.canonical_trace(attr.run_id))
    result = RegressionResult(spec=spec())
    assert not service.record_usage(old, result.usage)
    assert not service.complete(old, result, evidence)
    assert service.complete(new, result, evidence)
    assert not service.complete(old, result, evidence)
    assert store.get(accepted.id).status == "completed"


def test_duplicate_active_delivery_makes_no_second_call(setup_regression):
    _, store, _, fake, service = setup_regression
    accepted = submit(setup_regression)
    entered, release = Event(), Event()

    class BlockingGenerator:
        def generate(self, request):
            entered.set()
            assert release.wait(10)
            return fake.generate(request)

    service.generator = BlockingGenerator()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.process, accepted.id)
        assert entered.wait(10)
        try:
            with pytest.raises(RunClaimedError):
                service.process(accepted.id)
        finally:
            release.set()
        first.result(timeout=10)
    assert store.get(accepted.id).status == "completed" and len(fake.requests) == 1


def test_crash_redeliveries_have_durable_attempt_limit(setup_regression):
    _, store, _, fake, service = setup_regression
    accepted = submit(setup_regression)
    for _ in range(MAX_REGRESSION_ATTEMPTS):
        service.claim(accepted.id)
        with service.sessions.begin() as session:
            session.get(RegressionSpecOperation, accepted.id).claim_expires_at = datetime.now(
                UTC
            ) - timedelta(seconds=1)
    service.process(accepted.id)
    assert store.get(accepted.id).failure_code == "REGRESSION_ATTEMPTS_EXHAUSTED"
    assert not fake.requests


def test_result_write_rolls_back_atomically(setup_regression, postgres):
    _, store, _, _, service = setup_regression
    accepted = submit(setup_regression)
    engine, _, _ = postgres

    def reject_result(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE regression_specs") and "scenario_snapshot=" in statement:
            raise IntegrityError("private", {}, Exception("private"))

    event.listen(engine, "after_cursor_execute", reject_result)
    try:
        with pytest.raises(IntegrityError):
            service.process(accepted.id)
    finally:
        event.remove(engine, "after_cursor_execute", reject_result)
    result = store.get(accepted.id)
    assert result.status == "failed" and result.spec is None
    assert result.scenario_snapshot is None and result.completed_at is None


def test_api_submission_repeat_get_unknown_and_unfinished(setup_regression):
    attr_id, store, publisher, fake, service = setup_regression
    with TestClient(create_app(get_settings())) as client:
        client.app.state.regression_specs = store
        client.app.state.regression_publisher = publisher
        path = f"/api/v1/attributions/{attr_id}/regression-specs"
        response = client.post(path)
        assert response.status_code == 202
        assert client.post(path).status_code == 200
        service.process(publisher.calls[0])
        result = client.get(response.headers["location"])
        assert result.status_code == 200 and result.json()["disposition"] == "synthesizable"
        assert result.json()["scenario_snapshot"]["stimulus"] == "Return 4"
        assert "claim_token" not in result.json()
        assert (
            client.get(f"/api/v1/regression-specs/{uuid4()}").json()["error"]["code"]
            == "REGRESSION_SPEC_NOT_FOUND"
        )
        assert client.post(f"/api/v1/attributions/{uuid4()}/regression-specs").status_code == 404
        # Create an unfinished attribution on a separate normalized run.
        with service.sessions.begin() as session:
            run_id = session.get(Attribution, attr_id).run_id
            session.delete(session.get(Attribution, attr_id))
            session.flush()
            pending = Attribution(
                run_id=run_id,
                provider="openai",
                model="model",
                reasoning_effort="medium",
                prompt_version="attribution-v1",
            )
            session.add(pending)
            session.flush()
            pending_id = pending.id
        assert client.post(f"/api/v1/attributions/{pending_id}/regression-specs").status_code == 409
    assert len(fake.requests) == 1


def test_publication_failure_is_durable_and_api_returns_503(setup_regression):
    attr_id, store, publisher, fake, service = setup_regression
    publisher.fail = True
    with TestClient(create_app(get_settings())) as client:
        client.app.state.regression_specs = store
        client.app.state.regression_publisher = publisher
        response = client.post(f"/api/v1/attributions/{attr_id}/regression-specs")
    assert response.status_code == 503
    assert response.json()["error"]["regression_spec_id"] == str(publisher.calls[0])
    assert store.get(publisher.calls[0]).failure_code == "QUEUE_PUBLICATION_FAILED"
    service.process(publisher.calls[0])
    assert not fake.requests


def test_migration_consistency_constraints_and_downgrade(setup_regression, postgres):
    attr_id, store, _, _, service = setup_regression
    _, sessions, config = postgres
    command.check(config)

    accepted = submit(setup_regression)
    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.add(
            RegressionSpecOperation(
                attribution_id=attr_id,
                provider="openai",
                model="model",
                reasoning_effort="medium",
                prompt_version="regression-spec-v1",
            )
        )
    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.get(RegressionSpecOperation, accepted.id).status = "completed"
    attr = service.attributions.get(attr_id)
    original = service.runs.canonical_trace(attr.run_id)
    command.downgrade(config, "0003")
    assert service.attributions.get(attr_id).status == "completed"
    assert service.runs.canonical_trace(attr.run_id) == original
    command.upgrade(config, "head")
    command.check(config)


def test_api_rabbitmq_worker_with_fake_generation(
    setup_regression, integration_settings, monkeypatch
):
    if integration_settings.test_rabbitmq_url is None:
        pytest.skip("Set TEST_RABBITMQ_URL for the worker smoke test")
    attr_id, store, _, fake, _ = setup_regression
    settings = get_settings().model_copy(
        update={"rabbitmq_url": integration_settings.test_rabbitmq_url}
    )
    monkeypatch.setattr(
        "app.workers.regression_tasks.OpenAILLMClient", lambda *args, **kwargs: fake
    )
    app = create_celery_app(settings)
    queue = "traceguard_regression_test_" + uuid4().hex
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
            client.app.state.regression_specs = store
            client.app.state.regression_publisher = CeleryRegressionPublisher(app, 3)
            path = f"/api/v1/attributions/{attr_id}/regression-specs"
            response = client.post(path)
            assert response.status_code == 202
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                result = client.get(response.headers["location"]).json()
                if result["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.05)
            assert result["status"] == "completed" and result["disposition"] == "synthesizable"
            assert (
                result["input_tokens"] == 100
                and result["scenario_snapshot"]["stimulus"] == "Return 4"
            )
            assert client.post(path).status_code == 200 and len(fake.requests) == 1
    finally:
        app.close()
