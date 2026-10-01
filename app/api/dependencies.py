from typing import cast

from fastapi import Request

from app.integrations.task_queue import CeleryAttributionPublisher
from app.services.attributions import AttributionStore
from app.services.readiness import ReadinessService
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
