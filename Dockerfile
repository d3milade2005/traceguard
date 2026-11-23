FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY app ./app
RUN uv sync --locked --no-dev --no-editable --no-cache

FROM python:3.12-slim-bookworm AS runtime
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN groupadd --gid 10001 traceguard \
    && useradd --uid 10001 --gid traceguard --create-home traceguard
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY alembic.ini ./
COPY alembic ./alembic
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

FROM runtime AS validation-worker
COPY --from=docker:28.1.1-cli@sha256:f4ca5cab1815946d04c814d564828af0869c2fc23a7ef014d07cab1694d93cae /usr/local/bin/docker /usr/local/bin/docker
CMD ["celery", "-A", "app.workers.validation_celery_app:celery_app", "worker", "-Q", "traceguard.validation", "--concurrency=1", "--loglevel=INFO"]

FROM runtime AS application
