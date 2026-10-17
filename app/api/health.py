from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import get_readiness_service
from app.schemas.http import ErrorResponse, HealthResponse, ReadinessResponse
from app.services.readiness import ReadinessService

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


@router.get("/ready", response_model=ReadinessResponse, responses={503: {"model": ErrorResponse}})
def ready(
    service: Annotated[ReadinessService, Depends(get_readiness_service)],
) -> ReadinessResponse:
    service.check()
    return ReadinessResponse()
