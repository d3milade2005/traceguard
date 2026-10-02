import json
import logging
import sys
import traceback
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from urllib.parse import unquote, urlsplit

from sqlalchemy.engine import make_url

from app.core.config import Settings

log_context: ContextVar[Mapping[str, str]] = ContextVar("log_context", default=MappingProxyType({}))
LOG_FIELDS = (
    "request_id",
    "run_id",
    "task_id",
    "component",
    "source",
    "stage",
    "status",
    "duration_ms",
    "attribution_id",
    "regression_spec_id",
    "provider",
    "model",
    "prompt_version",
    "response_id",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "latency_ms",
)


@contextmanager
def bind_log_context(**values: str) -> Iterator[None]:
    token = log_context.set({**log_context.get(), **values})
    try:
        yield
    finally:
        log_context.reset(token)


class JsonFormatter(logging.Formatter):
    """Allowlist fields; preserve stack locations without exception payloads or locals."""

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.secrets = secrets

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage() if not record.exc_info else "Operation failed",
        }
        context = log_context.get()
        for field in LOG_FIELDS:
            value = getattr(record, field, context.get(field))
            if value is not None:
                payload[field] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception_type"] = record.exc_info[0].__name__
            payload["stack"] = [
                {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                for frame in traceback.extract_tb(record.exc_info[2])
            ]
        result = json.dumps(payload, ensure_ascii=False, default=str)
        for secret in self.secrets:
            if secret:
                result = result.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "[REDACTED]")
        return result


def configure_logging(settings: Settings) -> None:
    database_url = settings.database_url.get_secret_value()
    broker_url = settings.rabbitmq_url.get_secret_value()
    secrets = (
        database_url,
        broker_url,
        make_url(database_url).password or "",
        unquote(urlsplit(broker_url).password or ""),
        settings.openai_api_key.get_secret_value() if settings.openai_api_key else "",
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter(secrets))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)
    for name in ("uvicorn", "uvicorn.error", "celery", "celery.task"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    # Request middleware logs status and duration without URLs, query strings, or bodies.
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    # SDK debug logging can include request bodies; never inherit application DEBUG.
    for name in ("openai", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
