import logging
from uuid import UUID

from celery import Celery, Task

from app.core.config import Settings
from app.core.exceptions import ProcessingRetryError, RunClaimedError
from app.db.session import create_database_engine, create_session_factory
from app.integrations.execution.docker_runner import DockerValidationRunner
from app.services.validations import ValidationService

logger = logging.getLogger(__name__)
VALIDATION_TASK_NAME = "traceguard.validate_regression"
VALIDATION_QUEUE = "traceguard.validation"


def register_validation_task(app: Celery, settings: Settings) -> None:
    def validate_regression(task: Task, validation_id: str) -> None:
        engine = create_database_engine(settings)
        try:
            ValidationService(
                create_session_factory(engine), settings, DockerValidationRunner(settings)
            ).process(UUID(validation_id), final_attempt=task.request.retries >= 2)
        except (ProcessingRetryError, RunClaimedError) as exc:
            busy = isinstance(exc, RunClaimedError)
            limit = 6 if busy else 2
            if task.request.retries >= limit:
                logger.exception("Validation retries exhausted")
                raise
            delay = (
                exc.retry_after
                if isinstance(exc, RunClaimedError)
                else min(2 ** (task.request.retries + 1), 30)
            )
            raise task.retry(exc=exc, countdown=delay, max_retries=limit) from exc
        finally:
            engine.dispose()

    app.task(
        bind=True,
        shared=False,
        lazy=False,
        name=VALIDATION_TASK_NAME,
        acks_late=True,
        reject_on_worker_lost=True,
        max_retries=2,
        soft_time_limit=2 * settings.validation_timeout_seconds + 120,
        time_limit=2 * settings.validation_timeout_seconds + 150,
    )(validate_regression)
