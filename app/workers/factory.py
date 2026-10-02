from celery import Celery

from app.core.config import Settings
from app.workers import logging as worker_logging


def create_celery_app(settings: Settings) -> Celery:
    worker_logging.register_signals()
    app = Celery("traceguard", broker=settings.rabbitmq_url.get_secret_value())
    app.conf.update(
        accept_content=["json"],
        task_serializer="json",
        result_serializer="json",
        result_backend=None,
        task_ignore_result=True,
        task_store_errors_even_if_ignored=False,
        task_default_queue="traceguard",
        task_default_delivery_mode="persistent",
        task_create_missing_queues=False,
        task_acks_late=True,
        task_acks_on_failure_or_timeout=True,
        # Requeue on repeated process crashes can create poison-message loops.
        task_reject_on_worker_lost=False,
        worker_prefetch_multiplier=1,
        worker_concurrency=settings.worker_concurrency,
        worker_hijack_root_logger=False,
        worker_redirect_stdouts=False,
        worker_cancel_long_running_tasks_on_connection_loss=True,
        broker_connection_retry_on_startup=True,
        broker_connection_max_retries=5,
        broker_connection_timeout=settings.dependency_timeout_seconds,
        broker_heartbeat=30,
        broker_transport_options={"confirm_publish": True},
        task_publish_retry=True,
        task_publish_retry_policy={
            "max_retries": 3,
            "interval_start": 0,
            "interval_step": 0.5,
            "interval_max": 2,
        },
        enable_utc=True,
        timezone="UTC",
    )
    from app.workers.tasks import register_tasks

    register_tasks(app, settings)
    from app.workers.attribution_tasks import register_attribution_task

    register_attribution_task(app, settings)
    from app.workers.regression_tasks import register_regression_task

    register_regression_task(app, settings)
    return app
