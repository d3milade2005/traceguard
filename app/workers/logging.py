from celery import signals

from app.core.config import get_settings
from app.core.logging import configure_logging, log_context


def setup_worker_logging(**kwargs: object) -> None:
    configure_logging(get_settings())


def propagate_request_id(headers: dict[str, object] | None = None, **kwargs: object) -> None:
    request_id = log_context.get().get("request_id")
    if headers is not None and request_id:
        headers["request_id"] = request_id


def bind_task_context(
    task_id: str | None = None,
    task: object = None,
    kwargs: dict[str, object] | None = None,
    **extra: object,
) -> None:
    context: dict[str, str] = {}
    if task_id:
        context["task_id"] = task_id
    request = getattr(task, "request", None)
    headers = getattr(request, "headers", None)
    if isinstance(headers, dict) and isinstance(headers.get("request_id"), str):
        context["request_id"] = headers["request_id"]
    if kwargs and isinstance(kwargs.get("run_id"), str):
        context["run_id"] = str(kwargs["run_id"])
    log_context.set(context)


def clear_task_context(**kwargs: object) -> None:
    log_context.set({})


def register_signals() -> None:
    signals.setup_logging.connect(setup_worker_logging, weak=False)
    signals.before_task_publish.connect(propagate_request_id, weak=False)
    signals.task_prerun.connect(bind_task_context, weak=False)
    signals.task_postrun.connect(clear_task_context, weak=False)
