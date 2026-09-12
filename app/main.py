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
from app.services.readiness import ReadinessService


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
        try:
            yield
        finally:
            engine.dispose()

    # Debug never enables Starlette's traceback responses.
    app = FastAPI(
        title=settings.app_name,
        version=version("traceguard"),
        description="Post-mortem multi-agent trace diagnostics. Foundation phase.",
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
