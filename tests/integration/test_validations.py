import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from alembic import command
from celery.contrib.testing.worker import start_worker
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.exceptions import (
    ProcessingRetryError,
    RunClaimedError,
    SandboxUnavailableError,
    ValidationArtifactError,
    ValidationQueuePublishError,
)
from app.db.models import GeneratedTest, RegressionValidation
from app.integrations.task_queue import CeleryValidationPublisher
from app.main import create_app
from app.schemas.validations import RuntimeIdentity, SandboxLimits
from app.services.validations import ValidationService
from app.workers.factory import create_celery_app
from tests.integration.test_generated_tests import compilation_setup as compilation_setup
from tests.unit.test_validation import result

pytestmark = pytest.mark.integration


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.mode = "valid"

    def prepare(self, image_reference):
        return RuntimeIdentity(
            image_reference=image_reference,
            image_id="sha256:" + "a" * 64,
            bundle_digest="b" * 64,
            limits=SandboxLimits.from_settings(get_settings()),
        )

    def cleanup_expired(self):
        return None

    def cleanup_previous(self, validation_id, token):
        return None

    def execute(self, bundle, runtime, variant, validation_id, token):
        self.calls.append((variant, bundle.source))
        if self.mode == "unavailable":
            raise SandboxUnavailableError
        outcome = "passed" if variant == "corrected" else "assertion_failed"
        if self.mode == "no_reproduction":
            outcome = "passed"
        elif self.mode == "both_fail":
            outcome = "assertion_failed"
        elif self.mode == "timeout":
            outcome = "timeout"
        return result(variant, outcome)


class Publisher:
    def __init__(self, service):
        self.service = service
        self.calls = []
        self.fail = False

    def publish(self, validation_id, request_id):
        assert self.service.get(validation_id).status == "pending"
        self.calls.append(validation_id)
        if self.fail:
            raise ValidationQueuePublishError(validation_id=validation_id)


@pytest.fixture
def validation_setup(compilation_setup):
    parent_id, _, compiler_service = compilation_setup
    artifact, _ = compiler_service.compile(parent_id, "compile")
    fake = FakeRunner()
    service = ValidationService(compiler_service.sessions, get_settings(), fake)
    return artifact, service, Publisher(service), fake


@pytest.mark.parametrize(
    "mode,verdict",
    [
        ("valid", "valid"),
        ("no_reproduction", "not_reproduced"),
        ("both_fail", "not_discriminating"),
        ("timeout", "inconclusive"),
    ],
)
def test_persisted_verdicts_and_terminal_delivery_noop(validation_setup, mode, verdict):
    artifact, service, publisher, fake = validation_setup
    fake.mode = mode
    accepted, created = service.submit(artifact.id, "test", publisher)
    assert created
    service.process(accepted.id)
    service.process(accepted.id)
    persisted = service.get(accepted.id)
    assert persisted.status == "completed" and persisted.verdict == verdict
    assert [variant for variant, _ in fake.calls] == ["faulty", "corrected"]
    assert fake.calls[0][1] == fake.calls[1][1] == artifact.source_code
    assert persisted.source_digest == artifact.source_digest


def test_api_submission_read_repeat_and_unknown(validation_setup):
    artifact, service, publisher, _ = validation_setup
    with TestClient(create_app(get_settings())) as client:
        client.app.state.validations = service
        client.app.state.validation_publisher = publisher
        path = f"/api/v1/generated-tests/{artifact.id}/validations"
        response = client.post(path)
        assert response.status_code == 202
        assert client.post(path).status_code == 200
        service.process(publisher.calls[0])
        body = client.get(response.headers["location"]).json()
        assert (
            body["verdict"] == "valid" and body["faulty_execution"]["outcome"] == "assertion_failed"
        )
        assert "source_code" not in body and "claim_token" not in body
        assert client.get(f"/api/v1/validations/{uuid4()}").status_code == 404
        assert client.post(f"/api/v1/generated-tests/{uuid4()}/validations").status_code == 404
    assert len(publisher.calls) == 1


def test_concurrent_submission_and_duplicate_active_delivery(validation_setup):
    artifact, service, publisher, fake = validation_setup
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: service.submit(artifact.id, "concurrent", publisher), range(2))
        )
    assert results[0][0].id == results[1][0].id and len(publisher.calls) == 1
    entered, release = Event(), Event()
    original = fake.execute

    def blocking(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    fake.execute = blocking
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(service.process, publisher.calls[0])
        assert entered.wait(10)
        try:
            with pytest.raises(RunClaimedError):
                service.process(publisher.calls[0])
        finally:
            release.set()
        future.result(timeout=10)
    assert len(fake.calls) == 2


def test_stale_owner_cannot_write_or_release(validation_setup):
    artifact, service, publisher, fake = validation_setup
    accepted, _ = service.submit(artifact.id, "stale", publisher)
    old = service.claim(accepted.id)
    with service.sessions.begin() as session:
        session.get(RegressionValidation, accepted.id).claim_expires_at = datetime.now(
            UTC
        ) - timedelta(seconds=1)
    new = service.claim(accepted.id)
    assert not service.record_runtime(old, fake.prepare(service.settings.validation_image))
    service.release(accepted.id, old, "PRIVATE")
    assert service.record_runtime(new, fake.prepare(service.settings.validation_image))
    assert service.complete(
        new, result("faulty", "assertion_failed"), result("corrected", "passed")
    )
    assert not service.complete(old, result("faulty", "passed"), result("corrected", "passed"))
    assert service.get(accepted.id).verdict == "valid"


def test_artifact_digest_reverified_before_any_sandbox(validation_setup):
    artifact, service, publisher, fake = validation_setup
    accepted, _ = service.submit(artifact.id, "digest", publisher)
    with service.sessions.begin() as session:
        session.get(GeneratedTest, artifact.id).source_digest = "0" * 64
    service.process(accepted.id)
    assert service.get(accepted.id).failure_code == "VALIDATION_ARTIFACT_INVALID"
    assert not fake.calls
    with pytest.raises(ValidationArtifactError):
        service.submit(artifact.id, "bad", publisher)


def test_transient_retries_are_bounded_and_timeouts_are_terminal(validation_setup):
    artifact, service, publisher, fake = validation_setup
    accepted, _ = service.submit(artifact.id, "retry", publisher)
    fake.mode = "unavailable"
    with pytest.raises(ProcessingRetryError):
        service.process(accepted.id)
    assert service.get(accepted.id).status == "queued"
    with pytest.raises(ProcessingRetryError):
        service.process(accepted.id, final_attempt=True)
    assert service.get(accepted.id).failure_code == "VALIDATION_INFRASTRUCTURE_FAILED"
    service.process(accepted.id)
    assert len(fake.calls) == 2


def test_publication_failure_is_durable(validation_setup):
    artifact, service, publisher, fake = validation_setup
    publisher.fail = True
    with pytest.raises(ValidationQueuePublishError):
        service.submit(artifact.id, "broker", publisher)
    assert service.get(publisher.calls[0]).failure_code == "QUEUE_PUBLICATION_FAILED"
    service.process(publisher.calls[0])
    assert not fake.calls


def test_result_failure_rolls_back_both_executions(validation_setup, postgres):
    artifact, service, publisher, _ = validation_setup
    accepted, _ = service.submit(artifact.id, "atomic", publisher)
    engine, _, _ = postgres

    def reject(connection, cursor, statement, parameters, context, executemany):
        if (
            statement.startswith("UPDATE regression_validations")
            and "faulty_execution=" in statement
        ):
            raise IntegrityError("private", {}, Exception("secret"))

    event.listen(engine, "after_cursor_execute", reject)
    try:
        with pytest.raises(IntegrityError):
            service.process(accepted.id)
    finally:
        event.remove(engine, "after_cursor_execute", reject)
    row = service.get(accepted.id)
    assert (
        row.status == "failed" and row.faulty_execution is None and row.corrected_execution is None
    )


def test_migration_constraints_and_schema_consistency(validation_setup, postgres):
    artifact, service, publisher, _ = validation_setup
    engine, sessions, config = postgres
    command.check(config)
    accepted, _ = service.submit(artifact.id, "migration", publisher)
    service.process(accepted.id)
    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.get(RegressionValidation, accepted.id).verdict = "not_reproduced"
    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.add(
            RegressionValidation(
                generated_test_id=artifact.id,
                validator_version="regression-validator-v1",
                source_digest=artifact.source_digest,
                compiler_version=artifact.compiler_version,
                harness_identifier=artifact.harness_identifier,
                harness_version=artifact.harness_version,
                image_reference=service.settings.validation_image,
            )
        )
    assert (
        inspect(engine).get_foreign_keys("regression_validations")[0]["referred_table"]
        == "generated_tests"
    )
    command.downgrade(config, "0005")
    with sessions() as session:
        assert session.get(GeneratedTest, artifact.id).source_code == artifact.source_code
    assert "regression_validations" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    command.check(config)


def test_dedicated_rabbitmq_worker_flow_uses_fake_runner(
    validation_setup, integration_settings, monkeypatch
):
    if integration_settings.test_rabbitmq_url is None:
        pytest.skip("TEST_RABBITMQ_URL required")
    artifact, service, _, fake = validation_setup
    settings = get_settings().model_copy(
        update={"rabbitmq_url": integration_settings.test_rabbitmq_url}
    )
    monkeypatch.setattr(
        "app.workers.validation_tasks.DockerValidationRunner", lambda settings: fake
    )
    app = create_celery_app(settings, validation_worker=True)
    # A developer may already have the normal validation worker running. The
    # integration test must never publish to or consume its operational queue.
    from kombu import Queue

    queue_name = "traceguard_test_" + uuid4().hex
    app.conf.task_default_queue = queue_name
    app.conf.task_queues = (Queue(queue_name, auto_delete=True),)
    send_task = app.send_task

    def isolated_send_task(*args, **kwargs):
        kwargs["queue"] = Queue(queue_name, auto_delete=True)
        return send_task(*args, **kwargs)

    monkeypatch.setattr(app, "send_task", isolated_send_task)
    try:
        with start_worker(
            app,
            pool="solo",
            concurrency=1,
            queues=[queue_name],
            perform_ping_check=False,
            shutdown_timeout=30,
        ):
            accepted, _ = service.submit(artifact.id, "rabbitmq", CeleryValidationPublisher(app, 3))
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                row = service.get(accepted.id)
                if row.status in {"completed", "failed"}:
                    break
                time.sleep(0.05)
            assert row.status == "completed" and row.verdict == "valid"
    finally:
        app.close()
