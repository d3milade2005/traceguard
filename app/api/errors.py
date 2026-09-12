import logging
from http import HTTPStatus
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from app.core.exceptions import (
    ApplicationError,
    DependencyUnavailableError,
    InvalidTraceError,
    QueuePublishError,
    RunNotFoundError,
    TraceAdapterError,
    UnsupportedTraceSourceError,
)
from app.schemas.http import ErrorDetail, ErrorResponse

logger = logging.getLogger(__name__)


def error_response(
    request_id: str, status: int, code: str, message: str, run_id: UUID | None = None
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorDetail(code=code, message=message, request_id=request_id, run_id=run_id)
    )
    return JSONResponse(
        status_code=status,
        content=body.model_dump(mode="json", exclude_none=True),
        headers={"X-Request-ID": request_id},
    )


async def application_error_handler(request: Request, exc: ApplicationError) -> JSONResponse:
    status = {
        DependencyUnavailableError: 503,
        RunNotFoundError: 404,
        InvalidTraceError: 413,
        QueuePublishError: 503,
        TraceAdapterError: 422,
        UnsupportedTraceSourceError: 422,
    }.get(type(exc), 500)
    return error_response(
        request.state.request_id,
        status,
        exc.code,
        exc.public_message,
        exc.run_id if isinstance(exc, QueuePublishError) else None,
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Validation details can contain submitted secrets and provider payloads.
    return error_response(
        request.state.request_id, 422, "VALIDATION_ERROR", "The request is invalid."
    )


async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    response = error_response(
        request.state.request_id,
        exc.status_code,
        f"HTTP_{exc.status_code}",
        HTTPStatus(exc.status_code).phrase,
    )
    # Preserve protocol headers without reflecting arbitrary error details.
    for name, value in (exc.headers or {}).items():
        if name.lower() in {"allow", "www-authenticate", "retry-after"}:
            response.headers[name] = value
    return response


async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "Unhandled request error",
        exc_info=(type(exc), exc, exc.__traceback__),
        extra={"request_id": request.state.request_id},
    )
    return error_response(
        request.state.request_id, 500, "INTERNAL_ERROR", "An unexpected error occurred."
    )


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApplicationError, application_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, http_error_handler)  # type: ignore[arg-type]
