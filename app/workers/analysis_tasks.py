import logging
from uuid import UUID

from celery import Celery, Task
from sqlalchemy.exc import OperationalError, TimeoutError

from app.core.config import Settings
from app.db.session import create_database_engine, create_session_factory
from app.services.analysis_factory import create_analysis_service

logger = logging.getLogger(__name__)


def register_analysis_task(app: Celery, settings: Settings) -> None:
    def advance_analysis(task: Task, run_id: str) -> None:
        engine = create_database_engine(settings)
        service = create_analysis_service(create_session_factory(engine), settings, app)
        try:
            try:
                pending = service.advance(UUID(run_id))
            except (OperationalError, TimeoutError) as exc:
                logger.exception("Analysis database unavailable")
                raise task.retry(exc=exc, countdown=10, max_retries=720) from exc
            except Exception:
                logger.exception("Unexpected analysis failure")
                service.fail(UUID(run_id), "ANALYSIS_FAILED")
                raise
            if pending:
                if task.request.retries >= 720:
                    service.fail(UUID(run_id), "ANALYSIS_POLL_LIMIT")
                else:
                    raise task.retry(countdown=5, max_retries=720)
        finally:
            engine.dispose()

    app.task(
        bind=True,
        shared=False,
        lazy=False,
        name="traceguard.advance_analysis",
        acks_late=True,
        reject_on_worker_lost=True,
        max_retries=720,
        soft_time_limit=60,
        time_limit=90,
    )(advance_analysis)
