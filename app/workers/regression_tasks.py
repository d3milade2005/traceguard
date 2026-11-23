import logging
from uuid import UUID

from celery import Celery, Task

from app.core.config import Settings
from app.core.exceptions import ProcessingRetryError, RunClaimedError
from app.db.session import create_database_engine, create_session_factory
from app.integrations.llm.openai import OpenAILLMClient
from app.services.attributions import AttributionStore
from app.services.regression import RegressionService
from app.services.runs import RunService

logger = logging.getLogger(__name__)
REGRESSION_TASK_NAME = "traceguard.generate_regression_spec"


def register_regression_task(app: Celery, settings: Settings) -> None:
    def generate_regression_spec(task: Task, regression_spec_id: str) -> None:
        logger.info("Regression task received", extra={"stage": "received"})
        engine = create_database_engine(settings)
        try:
            sessions = create_session_factory(engine)
            service = RegressionService(
                sessions,
                RunService(sessions, settings.max_request_body_bytes),
                AttributionStore(sessions),
                OpenAILLMClient(
                    settings.openai_api_key,
                    timeout_seconds=settings.openai_timeout_seconds,
                    max_output_tokens=settings.openai_max_output_tokens,
                ),
                lease_seconds=settings.openai_timeout_seconds + 60,
                max_input_bytes=settings.regression_max_input_bytes,
                max_scenario_bytes=settings.regression_max_scenario_bytes,
            )
            service.process(UUID(regression_spec_id), final_attempt=task.request.retries >= 3)
        except (ProcessingRetryError, RunClaimedError) as exc:
            busy = isinstance(exc, RunClaimedError)
            limit = 6 if busy else 3
            if task.request.retries >= limit:
                logger.exception("Regression task retries exhausted")
                raise
            delay = (
                exc.retry_after
                if isinstance(exc, RunClaimedError)
                else min(2 ** (task.request.retries + 1), 30)
            )
            logger.warning("Regression retry scheduled", extra={"stage": "retry"})
            raise task.retry(exc=exc, countdown=delay, max_retries=limit) from exc
        finally:
            engine.dispose()

    app.task(
        bind=True,
        shared=False,
        lazy=False,
        name=REGRESSION_TASK_NAME,
        acks_late=True,
        reject_on_worker_lost=True,
        max_retries=3,
        soft_time_limit=settings.openai_timeout_seconds + 30,
        time_limit=settings.openai_timeout_seconds + 40,
    )(generate_regression_spec)
