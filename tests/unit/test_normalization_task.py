from unittest.mock import Mock
from uuid import uuid4

import pytest
from celery.exceptions import Retry

from app.core.exceptions import ProcessingRetryError, RunClaimedError
from app.workers.factory import create_celery_app


@pytest.fixture
def task_boundary(settings, monkeypatch):
    service = Mock()
    engine = Mock()
    monkeypatch.setattr("app.workers.tasks.create_database_engine", Mock(return_value=engine))
    monkeypatch.setattr("app.workers.tasks.create_session_factory", Mock())
    monkeypatch.setattr("app.workers.tasks.NormalizationService", Mock(return_value=service))
    app = create_celery_app(settings)
    task = app.tasks["traceguard.process_run"]
    yield task, service, engine
    app.close()


def test_thin_task_calls_service_with_identifier(task_boundary):
    task, service, engine = task_boundary
    run_id = uuid4()
    task.run(str(run_id))
    service.process.assert_called_once_with(run_id, final_attempt=False)
    engine.dispose.assert_called_once()


@pytest.mark.parametrize(
    ("error", "countdown", "limit"),
    [(ProcessingRetryError(), 2, 3), (RunClaimedError(43), 43, 6)],
)
def test_task_delays_retryable_failure(task_boundary, monkeypatch, error, countdown, limit):
    task, service, engine = task_boundary
    service.process.side_effect = error
    retry = Mock(side_effect=Retry())
    monkeypatch.setattr(task, "retry", retry)
    with pytest.raises(Retry):
        task.run(str(uuid4()))
    retry.assert_called_once_with(exc=error, countdown=countdown, max_retries=limit)
    engine.dispose.assert_called_once()


def test_final_attempt_does_not_publish_another_retry(task_boundary, monkeypatch):
    task, service, engine = task_boundary
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


def test_permanent_failure_does_not_retry(task_boundary, monkeypatch):
    # The service has already recorded a permanent input failure before returning.
    task, _, engine = task_boundary
    retry = Mock()
    monkeypatch.setattr(task, "retry", retry)
    task.run(str(uuid4()))
    retry.assert_not_called()
    engine.dispose.assert_called_once()
