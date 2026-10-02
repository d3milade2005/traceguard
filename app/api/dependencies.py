from typing import cast

from fastapi import Request

from app.integrations.task_queue import CeleryAttributionPublisher, CeleryRegressionPublisher
from app.services.attributions import AttributionStore
from app.services.generated_tests import GeneratedTestService
from app.services.readiness import ReadinessService
from app.services.regression_specs import RegressionStore
from app.services.runs import RunService
from app.services.submission import SubmissionService


def get_readiness_service(request: Request) -> ReadinessService:
    return cast(ReadinessService, request.app.state.readiness)


def get_run_service(request: Request) -> RunService:
    return cast(RunService, request.app.state.runs)


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
