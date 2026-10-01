from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from app.api.errors import install_exception_handlers
from app.api.health import router as health_router
from app.api.middleware import (
    RequestBodyLimitMiddleware,
    RequestContextMiddleware,
    UnexpectedErrorMiddleware,
)
from app.api.v1.router import router as v1_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import create_database_engine, create_session_factory
from app.integrations.task_queue import CeleryAttributionPublisher, CeleryRunPublisher
from app.integrations.traces.sources import trace_adapters
from app.services.attributions import AttributionStore
from app.services.readiness import ReadinessService
from app.services.runs import RunService
from app.services.submission import SubmissionService
from app.workers.factory import create_celery_app


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_database_engine(settings)
        app.state.sessions = create_session_factory(engine)
        app.state.readiness = ReadinessService(
            engine, settings.rabbitmq_url.get_secret_value(), settings.dependency_timeout_seconds
        )
        queue = create_celery_app(settings)
        app.state.runs = RunService(app.state.sessions, settings.max_request_body_bytes)
        app.state.attributions = AttributionStore(app.state.sessions)
        app.state.attribution_publisher = CeleryAttributionPublisher(
            queue, settings.dependency_timeout_seconds
        )
        app.state.submission = SubmissionService(
            app.state.runs,
            CeleryRunPublisher(queue, settings.dependency_timeout_seconds),
            trace_adapters(),
        )
        try:
            yield
        finally:
            queue.close()
            engine.dispose()

    # Debug never enables Starlette's traceback responses.
    app = FastAPI(
        title=settings.app_name,
        version=version("traceguard"),
        description="Multi-agent trace diagnostics and asynchronous normalization.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    install_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(v1_router, prefix=settings.api_v1_prefix)
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(UnexpectedErrorMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestContextMiddleware)
    return app
