from uuid import UUID

from celery import Celery
from kombu.exceptions import OperationalError

from app.core.exceptions import QueuePublishError
from app.workers.tasks import TASK_NAME


class CeleryRunPublisher:
    def __init__(self, app: Celery, timeout: int) -> None:
        self.app = app
        self.timeout = timeout

    def publish(self, run_id: UUID, request_id: str | None) -> None:
        try:
            self.app.send_task(
                TASK_NAME,
                kwargs={"run_id": str(run_id)},
                headers={"request_id": request_id} if request_id else {},
                task_id=str(run_id),
                timeout=self.timeout,
                confirm_timeout=self.timeout,
            )
        except (OperationalError, OSError) as exc:
            raise QueuePublishError(run_id=run_id) from exc
