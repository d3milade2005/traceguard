from uuid import UUID

from celery import Celery
from kombu import Queue

from app.core.exceptions import (
    AttributionQueuePublishError,
    QueuePublishError,
    RegressionQueuePublishError,
    ValidationQueuePublishError,
)
from app.workers.attribution_tasks import ATTRIBUTION_TASK_NAME
from app.workers.regression_tasks import REGRESSION_TASK_NAME
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
        except Exception as exc:
            # The external publication boundary must preserve the durable Run even
            # for unexpected transport/serialization errors or uncertain confirms.
            raise QueuePublishError(run_id=run_id) from exc


class CeleryAnalysisPublisher:
    def __init__(self, app: Celery, timeout: int) -> None:
        self.app = app
        self.timeout = timeout

    def publish(self, run_id: UUID, request_id: str | None) -> None:
        try:
            self.app.send_task(
                "traceguard.advance_analysis",
                kwargs={"run_id": str(run_id)},
                headers={"request_id": request_id} if request_id else {},
                timeout=self.timeout,
                confirm_timeout=self.timeout,
            )
        except Exception as exc:
            raise QueuePublishError(run_id=run_id) from exc


class CeleryAttributionPublisher:
    def __init__(self, app: Celery, timeout: int) -> None:
        self.app = app
        self.timeout = timeout

    def publish(self, attribution_id: UUID, request_id: str | None) -> None:
        try:
            self.app.send_task(
                ATTRIBUTION_TASK_NAME,
                kwargs={"attribution_id": str(attribution_id)},
                headers={"request_id": request_id} if request_id else {},
                task_id=str(attribution_id),
                timeout=self.timeout,
                confirm_timeout=self.timeout,
            )
        except Exception as exc:
            raise AttributionQueuePublishError(attribution_id=attribution_id) from exc


class CeleryRegressionPublisher:
    def __init__(self, app: Celery, timeout: int) -> None:
        self.app = app
        self.timeout = timeout

    def publish(self, regression_spec_id: UUID, request_id: str | None) -> None:
        try:
            self.app.send_task(
                REGRESSION_TASK_NAME,
                kwargs={"regression_spec_id": str(regression_spec_id)},
                headers={"request_id": request_id} if request_id else {},
                task_id=str(regression_spec_id),
                timeout=self.timeout,
                confirm_timeout=self.timeout,
            )
        except Exception as exc:
            raise RegressionQueuePublishError(regression_spec_id=regression_spec_id) from exc


class CeleryValidationPublisher:
    def __init__(self, app: Celery, timeout: int) -> None:
        self.app = app
        self.timeout = timeout

    def publish(self, validation_id: UUID, request_id: str | None) -> None:
        try:
            self.app.send_task(
                "traceguard.validate_regression",
                queue=Queue("traceguard.validation"),
                kwargs={"validation_id": str(validation_id)},
                headers={"request_id": request_id} if request_id else {},
                task_id=str(validation_id),
                timeout=self.timeout,
                confirm_timeout=self.timeout,
            )
        except Exception as exc:
            raise ValidationQueuePublishError(validation_id=validation_id) from exc
