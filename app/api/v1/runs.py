from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from app.api.dependencies import get_run_service, get_submission_service
from app.schemas.runs import RunAccepted, RunSubmissionQuery, RunSummary
from app.services.runs import RunService
from app.services.submission import SubmissionService

router = APIRouter(prefix="/runs", tags=["runs"])


@router.post(
    "",
    status_code=202,
    response_model=RunAccepted,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {"type": "object"}}},
            "description": "Original UTF-8 record: question and history only; no benchmark labels.",
        }
    },
)
async def submit_run(
    request: Request,
    response: Response,
    query: Annotated[RunSubmissionQuery, Query()],
    service: Annotated[SubmissionService, Depends(get_submission_service)],
) -> RunAccepted:
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415)
    result = await run_in_threadpool(
        service.submit, query.source, await request.body(), request.state.request_id
    )
    response.headers["Location"] = str(request.url_for("get_run", run_id=str(result.id)))
    return result


@router.get("/{run_id}", response_model=RunSummary)
def get_run(run_id: UUID, service: Annotated[RunService, Depends(get_run_service)]) -> RunSummary:
    return service.summary(run_id)
