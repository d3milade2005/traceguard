import logging

from kombu import Connection
from kombu.exceptions import OperationalError

from app.core.exceptions import DependencyUnavailableError

logger = logging.getLogger(__name__)


def check_broker(url: str, timeout: int) -> None:
    """Check authenticated AMQP connectivity without publishing or declaring queues."""
    try:
        with Connection(url, connect_timeout=timeout, heartbeat=0) as connection:
            connection.ensure_connection(max_retries=0, timeout=timeout)
    except (OperationalError, OSError) as exc:
        logger.warning("Broker readiness failed", exc_info=True, extra={"component": "rabbitmq"})
        raise DependencyUnavailableError from exc
