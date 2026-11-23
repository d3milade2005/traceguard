import logging
from uuid import UUID

from celery import Celery, Task
from celery.exceptions import Retry
from kombu.exceptions import OperationalError

from app.core.config import Settings
from app.core.exceptions import ProcessingRetryError, RunClaimedError, RunNotFoundError
from app.db.session import create_database_engine, create_session_factory
from app.integrations.traces.sources import trace_adapters
from app.services.normalization import NormalizationService

logger = logging.getLogger(__name__)
TASK_NAME = "traceguard.process_run"


def register_tasks(app: Celery, settings: Settings) -> None:
    def process_run(task: Task, run_id: str) -> None:
        logger.info("Task received", extra={"stage": "received"})
        # Construct in the worker process, never share a pre-fork connection pool.
        engine = create_database_engine(settings)
        service = NormalizationService(
            create_session_factory(engine),
            trace_adapters(),
            lease_seconds=settings.normalization_timeout_seconds + 30,
            max_trace_bytes=settings.max_request_body_bytes,
        )
        try:
            service.process(UUID(run_id), final_attempt=task.request.retries >= 3)
        except RunNotFoundError:
            logger.exception("Task references an unknown run")
            raise
        except (ProcessingRetryError, RunClaimedError) as exc:
            busy = isinstance(exc, RunClaimedError)
            limit = 6 if busy else 3
            if task.request.retries >= limit:
                logger.exception("Task retries exhausted")
                raise
            delay = (
                exc.retry_after
                if isinstance(exc, RunClaimedError)
                else min(2 ** (task.request.retries + 1), 30)
            )
            logger.warning("Task retry scheduled", extra={"stage": "retry"})
            try:
                raise task.retry(exc=exc, countdown=delay, max_retries=limit)
            except Retry:
                raise
            except (OperationalError, OSError):
                logger.exception("Retry publication failed; database state requires recovery")
                raise
        finally:
            engine.dispose()

    app.task(
        bind=True,
        shared=False,
        lazy=False,
        name=TASK_NAME,
        acks_late=True,
        reject_on_worker_lost=True,
        soft_time_limit=settings.normalization_timeout_seconds,
        time_limit=settings.normalization_timeout_seconds + 10,
        max_retries=3,
    )(process_run)
