from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from app.api.dependencies import get_analysis_service
from app.integrations.task_queue import CeleryAnalysisPublisher
from app.schemas.analyses import AnalysisRecord
from app.services.analyses import AnalysisService

router = APIRouter(tags=["analysis"])


@router.post("/runs/{run_id}/analysis", response_model=AnalysisRecord, status_code=202)
def request_analysis(
    run_id: UUID,
    request: Request,
    response: Response,
    service: Annotated[AnalysisService, Depends(get_analysis_service)],
) -> AnalysisRecord:
    settings = request.app.state.settings
    result, created = service.submit(
        run_id,
        request.state.request_id,
        CeleryAnalysisPublisher(request.app.state.queue, settings.dependency_timeout_seconds),
        model=settings.openai_model,
        reasoning_effort=settings.openai_reasoning_effort,
    )
    response.status_code = 202 if created else 200
    response.headers["Location"] = str(request.url_for("get_analysis", run_id=str(run_id)))
    return result


@router.get("/runs/{run_id}/analysis", response_model=AnalysisRecord)
def get_analysis(
    run_id: UUID,
    service: Annotated[AnalysisService, Depends(get_analysis_service)],
) -> AnalysisRecord:
    return service.get(run_id)
