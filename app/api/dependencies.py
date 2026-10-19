from typing import cast

from fastapi import Request, Response

from app.core.exceptions import CsrfError
from app.integrations.task_queue import (
    CeleryAttributionPublisher,
    CeleryRegressionPublisher,
    CeleryValidationPublisher,
)
from app.schemas.auth import UserProfile
from app.services.analyses import AnalysisService
from app.services.attributions import AttributionStore
from app.services.auth import AuthService
from app.services.generated_tests import GeneratedTestService
from app.services.readiness import ReadinessService
from app.services.regression_specs import RegressionStore
from app.services.run_reads import RunReadService
from app.services.runs import RunService
from app.services.submission import SubmissionService
from app.services.validations import ValidationService


def get_auth_service(request: Request) -> AuthService:
    return cast(AuthService, request.app.state.auth)


def check_csrf(request: Request) -> None:
    settings = request.app.state.settings
    if request.headers.get("origin") not in {settings.frontend_origin, settings.backend_origin}:
        raise CsrfError


def require_user(request: Request, response: Response) -> UserProfile | None:
    settings = request.app.state.settings
    response.headers["Cache-Control"] = "no-store"
    if (
        settings.app_env == "development"
        and settings.dev_read_access
        and request.method in {"GET", "HEAD"}
        and not request.url.path.startswith(f"{settings.api_v1_prefix}/auth/")
    ):
        return None
    user = get_auth_service(request).authenticate(request.cookies.get(settings.session_cookie_name))
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_csrf(request)
    response.headers["Cache-Control"] = "no-store"
    return user


def get_readiness_service(request: Request) -> ReadinessService:
    return cast(ReadinessService, request.app.state.readiness)


def get_analysis_service(request: Request) -> AnalysisService:
    return cast(AnalysisService, request.app.state.analyses)


def get_run_service(request: Request) -> RunService:
    return cast(RunService, request.app.state.runs)


def get_run_reads(request: Request) -> RunReadService:
    return cast(RunReadService, request.app.state.run_reads)


def get_submission_service(request: Request) -> SubmissionService:
    return cast(SubmissionService, request.app.state.submission)


def get_attribution_store(request: Request) -> AttributionStore:
    return cast(AttributionStore, request.app.state.attributions)


def get_attribution_publisher(request: Request) -> CeleryAttributionPublisher:
    return cast(CeleryAttributionPublisher, request.app.state.attribution_publisher)


def get_regression_store(request: Request) -> RegressionStore:
    return cast(RegressionStore, request.app.state.regression_specs)


def get_regression_publisher(request: Request) -> CeleryRegressionPublisher:
    return cast(CeleryRegressionPublisher, request.app.state.regression_publisher)


def get_generated_test_service(request: Request) -> GeneratedTestService:
    return cast(GeneratedTestService, request.app.state.generated_tests)


def get_validation_service(request: Request) -> ValidationService:
    return cast(ValidationService, request.app.state.validations)


def get_validation_publisher(request: Request) -> CeleryValidationPublisher:
    return cast(CeleryValidationPublisher, request.app.state.validation_publisher)
