"""Remove only expired TraceGuard sandboxes after a runner crash or daemon outage."""

from app.core.config import get_settings
from app.integrations.execution.docker_runner import DockerValidationRunner


def main() -> None:
    DockerValidationRunner(get_settings()).cleanup_expired()


if __name__ == "__main__":
    main()
