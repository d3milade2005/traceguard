import logging
from uuid import UUID

from celery import Celery, Task

from app.core.config import Settings
from app.core.exceptions import ProcessingRetryError, RunClaimedError
from app.db.session import create_database_engine, create_session_factory
from app.integrations.llm.openai import OpenAILLMClient
from app.services.attribution import AttributionService
from app.services.context import ContextManager
from app.services.runs import RunService

logger = logging.getLogger(__name__)
ATTRIBUTION_TASK_NAME = "traceguard.attribute_run"


def register_attribution_task(app: Celery, settings: Settings) -> None:
    def attribute_run(task: Task, attribution_id: str) -> None:
        logger.info("Attribution task received", extra={"stage": "received"})
        engine = create_database_engine(settings)
        try:
            sessions = create_session_factory(engine)
            service = AttributionService(
                sessions,
                RunService(sessions, settings.max_request_body_bytes),
                ContextManager(settings.analysis_context),
                OpenAILLMClient(
                    settings.openai_api_key,
                    timeout_seconds=settings.openai_timeout_seconds,
                    max_output_tokens=settings.openai_max_output_tokens,
                ),
                lease_seconds=settings.openai_timeout_seconds + 60,
            )
            service.process(UUID(attribution_id), final_attempt=task.request.retries >= 3)
        except (ProcessingRetryError, RunClaimedError) as exc:
            busy = isinstance(exc, RunClaimedError)
            limit = 6 if busy else 3
            if task.request.retries >= limit:
                logger.exception("Attribution task retries exhausted")
                raise
            delay = (
                exc.retry_after
                if isinstance(exc, RunClaimedError)
                else min(2 ** (task.request.retries + 1), 30)
            )
            logger.warning("Attribution retry scheduled", extra={"stage": "retry"})
            raise task.retry(exc=exc, countdown=delay, max_retries=limit) from exc
        finally:
            engine.dispose()

    app.task(
        bind=True,
        shared=False,
        lazy=False,
        name=ATTRIBUTION_TASK_NAME,
        acks_late=True,
        reject_on_worker_lost=True,
        max_retries=3,
        soft_time_limit=settings.openai_timeout_seconds + 30,
        time_limit=settings.openai_timeout_seconds + 40,
    )(attribute_run)
