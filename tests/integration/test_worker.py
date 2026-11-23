import time
from uuid import uuid4

import pytest
from celery.contrib.testing.worker import start_worker
from kombu import Queue

from app.core.config import get_settings
from app.core.run_status import RunStatus
from app.integrations.task_queue import CeleryRunPublisher
from app.integrations.traces.sources import trace_adapters
from app.services.runs import RunService
from app.services.submission import SubmissionService
from app.workers.factory import create_celery_app

pytestmark = pytest.mark.integration


def test_real_rabbitmq_task_normalizes_persisted_run(postgres, integration_settings):
    if integration_settings.test_rabbitmq_url is None:
        pytest.skip("Set TEST_RABBITMQ_URL to run the live worker test")
    _, sessions, _ = postgres
    settings = get_settings().model_copy(
        update={"rabbitmq_url": integration_settings.test_rabbitmq_url}
    )
    app = create_celery_app(settings)
    # The test owns a separate queue; never consume existing developer tasks.
    queue = "traceguard_test_" + uuid4().hex
    app.conf.task_default_queue = queue
    app.conf.task_queues = (Queue(queue, auto_delete=True),)
    runs = RunService(sessions, settings.max_request_body_bytes)
    publisher = CeleryRunPublisher(app, 3)
    submission = SubmissionService(runs, publisher, trace_adapters())
    raw = b' {"question":"Q","history":[{"role":"human","content":""}]}\r\n'
    try:
        with start_worker(
            app,
            pool="solo",
            concurrency=1,
            queues=[queue],
            perform_ping_check=False,
            shutdown_timeout=15,
        ):
            result = submission.submit("who_when.hand_crafted", raw, "live-worker-test")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                summary = runs.summary(result.id)
                if summary.status in {RunStatus.NORMALIZED, RunStatus.FAILED}:
                    break
                time.sleep(0.05)
            assert summary.status == RunStatus.NORMALIZED
            assert summary.normalized_step_count == 1
            assert runs.get(result.id).original_trace == raw
    finally:
        app.close()
