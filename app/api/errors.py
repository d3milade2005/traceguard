import logging
from http import HTTPStatus
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from app.core.exceptions import (
    AnalysisNotFoundError,
    ApplicationError,
    AttributionNotCompletedError,
    AttributionNotFoundError,
    AttributionQueuePublishError,
    AuthConfigurationError,
    AuthenticationRequiredError,
    CompilationError,
    CompilationInputError,
    CompilationInvariantError,
    CompilationPersistenceError,
    CsrfError,
    DependencyUnavailableError,
    GeneratedSafetyError,
    GeneratedSyntaxError,
    GeneratedTestNotFoundError,
    GitHubUnavailableError,
    InvalidScenarioSnapshotError,
    InvalidTraceError,
    OAuthStateError,
    QueuePublishError,
    RegressionNotFoundError,
    RegressionNotSynthesizableError,
    RegressionQueuePublishError,
    ReplayContractError,
    RunNotFoundError,
    RunNotNormalizedError,
    SubmissionConflictError,
    TraceAdapterError,
    UnsafeRedirectError,
    UnsupportedAssertionError,
    UnsupportedSpecVersionError,
    UnsupportedTraceSourceError,
    ValidationArtifactError,
    ValidationNotFoundError,
    ValidationPersistenceError,
    ValidationQueuePublishError,
)
from app.schemas.http import ErrorDetail, ErrorResponse

logger = logging.getLogger(__name__)


def error_response(
    request_id: str,
    status: int,
    code: str,
    message: str,
    run_id: UUID | None = None,
    attribution_id: UUID | None = None,
    regression_spec_id: UUID | None = None,
    generated_test_id: UUID | None = None,
    validation_id: UUID | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            request_id=request_id,
            run_id=run_id,
            attribution_id=attribution_id,
            regression_spec_id=regression_spec_id,
            generated_test_id=generated_test_id,
            validation_id=validation_id,
        )
    )
    return JSONResponse(
        status_code=status,
        content=body.model_dump(mode="json", exclude_none=True),
        headers={"X-Request-ID": request_id, "Cache-Control": "no-store"},
    )


async def application_error_handler(request: Request, exc: ApplicationError) -> JSONResponse:
    status = {
        AuthenticationRequiredError: 401,
        CsrfError: 403,
        OAuthStateError: 400,
        UnsafeRedirectError: 400,
        AuthConfigurationError: 503,
        GitHubUnavailableError: 503,
        AnalysisNotFoundError: 404,
        DependencyUnavailableError: 503,
        RunNotFoundError: 404,
        InvalidTraceError: 413,
        QueuePublishError: 503,
        TraceAdapterError: 422,
        UnsupportedTraceSourceError: 422,
        AttributionNotFoundError: 404,
        AttributionQueuePublishError: 503,
        RunNotNormalizedError: 409,
        SubmissionConflictError: 409,
        AttributionNotCompletedError: 409,
        RegressionNotFoundError: 404,
        RegressionQueuePublishError: 503,
        RegressionNotSynthesizableError: 409,
        UnsupportedSpecVersionError: 422,
        CompilationInputError: 422,
        ReplayContractError: 422,
        UnsupportedAssertionError: 422,
        InvalidScenarioSnapshotError: 422,
        CompilationInvariantError: 500,
        GeneratedSafetyError: 500,
        GeneratedSyntaxError: 500,
        GeneratedTestNotFoundError: 404,
        CompilationPersistenceError: 503,
        ValidationArtifactError: 422,
        ValidationNotFoundError: 404,
        ValidationPersistenceError: 503,
        ValidationQueuePublishError: 503,
    }.get(type(exc), 500)
    return error_response(
        request.state.request_id,
        status,
        exc.code,
        exc.public_message,
        exc.run_id if isinstance(exc, QueuePublishError) else None,
        exc.attribution_id if isinstance(exc, AttributionQueuePublishError) else None,
        exc.regression_spec_id if isinstance(exc, RegressionQueuePublishError) else None,
        exc.generated_test_id if isinstance(exc, CompilationError) else None,
        exc.validation_id if isinstance(exc, ValidationQueuePublishError) else None,
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
