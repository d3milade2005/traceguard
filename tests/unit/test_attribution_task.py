from unittest.mock import Mock
from uuid import uuid4

import pytest
from celery.exceptions import Retry

from app.core.exceptions import AttributionQueuePublishError, ProcessingRetryError, RunClaimedError
from app.integrations.task_queue import CeleryAttributionPublisher
from app.workers.attribution_tasks import ATTRIBUTION_TASK_NAME
from app.workers.factory import create_celery_app


@pytest.fixture
def attribution_task(settings, monkeypatch):
    service, engine = Mock(), Mock()
    monkeypatch.setattr(
        "app.workers.attribution_tasks.create_database_engine", Mock(return_value=engine)
    )
    monkeypatch.setattr("app.workers.attribution_tasks.create_session_factory", Mock())
    monkeypatch.setattr(
        "app.workers.attribution_tasks.AttributionService", Mock(return_value=service)
    )
    app = create_celery_app(settings)
    yield app.tasks[ATTRIBUTION_TASK_NAME], service, engine
    app.close()


def test_task_uses_identifier_and_disposes_engine(attribution_task):
    task, service, engine = attribution_task
    attribution_id = uuid4()
    task.run(str(attribution_id))
    service.process.assert_called_once_with(attribution_id, final_attempt=False)
    engine.dispose.assert_called_once()


@pytest.mark.parametrize(
    "error,countdown,limit",
    [(ProcessingRetryError(), 2, 3), (RunClaimedError(43), 43, 6)],
)
def test_transient_and_busy_delivery_retry_separately(
    attribution_task, monkeypatch, error, countdown, limit
):
    task, service, engine = attribution_task
    service.process.side_effect = error
    retry = Mock(side_effect=Retry())
    monkeypatch.setattr(task, "retry", retry)
    with pytest.raises(Retry):
        task.run(str(uuid4()))
    retry.assert_called_once_with(exc=error, countdown=countdown, max_retries=limit)
    engine.dispose.assert_called_once()


def test_final_attempt_does_not_retry_again(attribution_task, monkeypatch):
    task, service, engine = attribution_task
    service.process.side_effect = ProcessingRetryError
    retry = Mock()
    monkeypatch.setattr(task, "retry", retry)
    task.push_request(retries=3)
    try:
        with pytest.raises(ProcessingRetryError):
            task.run(str(uuid4()))
        assert service.process.call_args.kwargs["final_attempt"] is True
    finally:
        task.pop_request()
    retry.assert_not_called()
    engine.dispose.assert_called_once()


def test_unexpected_worker_error_is_not_retried(attribution_task, monkeypatch):
    task, service, engine = attribution_task
    service.process.side_effect = RuntimeError("private payload")
    retry = Mock()
    monkeypatch.setattr(task, "retry", retry)
    with pytest.raises(RuntimeError):
        task.run(str(uuid4()))
    retry.assert_not_called()
    engine.dispose.assert_called_once()


def test_queue_contains_only_safe_identifier_and_correlation():
    app = Mock()
    attribution_id = uuid4()
    CeleryAttributionPublisher(app, 3).publish(attribution_id, "request-1")
    app.send_task.assert_called_once_with(
        ATTRIBUTION_TASK_NAME,
        kwargs={"attribution_id": str(attribution_id)},
        headers={"request_id": "request-1"},
        task_id=str(attribution_id),
        timeout=3,
        confirm_timeout=3,
    )


def test_queue_translates_transport_failure():
    app = Mock()
    app.send_task.side_effect = RuntimeError("transport secret")
    attribution_id = uuid4()
    with pytest.raises(AttributionQueuePublishError) as caught:
        CeleryAttributionPublisher(app, 3).publish(attribution_id, None)
    assert caught.value.attribution_id == attribution_id
    assert "transport secret" not in str(caught.value)
