from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from kombu.exceptions import OperationalError
from sqlalchemy.exc import OperationalError as DatabaseOperationalError

from app.core.exceptions import DependencyUnavailableError, InvalidTraceError, RunNotFoundError
from app.core.logging import bind_log_context, log_context
from app.db.session import check_database
from app.integrations.broker import check_broker
from app.schemas.runs import RunCreate
from app.services.runs import RunService
from app.workers.logging import bind_task_context, clear_task_context, propagate_request_id


def test_database_errors_are_translated():
    engine = Mock()
    engine.connect.side_effect = DatabaseOperationalError("private SQL", {}, Exception("secret"))
    with pytest.raises(DependencyUnavailableError) as failure:
        check_database(engine)
    assert isinstance(failure.value.__cause__, DatabaseOperationalError)


def test_broker_errors_are_translated(monkeypatch):
    connection = Mock()
    connection.return_value.__enter__ = Mock(side_effect=OperationalError("private"))
    connection.return_value.__exit__ = Mock()
    monkeypatch.setattr("app.integrations.broker.Connection", connection)
    with pytest.raises(DependencyUnavailableError):
        check_broker("amqp://test:test@localhost:5672/traceguard", 1)


def test_oversized_trace_rejected_before_transaction():
    sessions = Mock()
    service = RunService(sessions, max_trace_bytes=1)
    with pytest.raises(InvalidTraceError):
        service.create(RunCreate(source="test", failure_signal="timeout", original_trace=b"{}"))
    sessions.begin.assert_not_called()


def test_unknown_run_raises_application_error():
    sessions = Mock()
    session = Mock()
    sessions.return_value.__enter__ = Mock(return_value=session)
    sessions.return_value.__exit__ = Mock(return_value=False)
    session.get.return_value = None
    with pytest.raises(RunNotFoundError):
        RunService(sessions, 100).get(uuid4())


def test_failure_code_cannot_contain_exception_text():
    sessions = Mock()
    with pytest.raises(ValueError):
        RunService(sessions, 100).record_failure(uuid4(), "password=secret")
    sessions.begin.assert_not_called()


def test_celery_configuration_and_normalization_task(settings):
    from app.workers.celery_app import create_celery_app

    celery = create_celery_app(settings)
    assert celery.conf.accept_content == ["json"]
    assert celery.conf.result_backend is None
    assert celery.conf.task_ignore_result
    assert celery.conf.broker_connection_max_retries == 5
    assert celery.conf.worker_prefetch_multiplier == 1
    assert celery.conf.broker_transport_options["confirm_publish"]
    assert {name for name in celery.tasks if not name.startswith("celery.")} == {
        "traceguard.process_run"
    }
    assert celery.tasks["traceguard.process_run"].reject_on_worker_lost
    assert "traceguard" in celery.amqp.queues


def test_task_correlation_is_propagated_and_cleared():
    headers = {}
    with bind_log_context(request_id="request-1"):
        propagate_request_id(headers)
    assert headers == {"request_id": "request-1"}
    task = SimpleNamespace(request=SimpleNamespace(headers=headers))
    bind_task_context("task-1", task, kwargs={"run_id": "run-1"})
    assert log_context.get() == {"request_id": "request-1", "task_id": "task-1", "run_id": "run-1"}
    clear_task_context()
    assert not log_context.get()
