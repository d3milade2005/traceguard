from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from app.api.dependencies import get_run_reads, get_run_service, get_submission_service
from app.schemas.runs import RunAccepted, RunListQuery, RunPage, RunSubmissionQuery, RunSummary
from app.schemas.traces import CanonicalTrace
from app.services.run_reads import RunReadService
from app.services.runs import RunService
from app.services.submission import SubmissionService

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("", response_model=RunPage)
def list_runs(
    query: Annotated[RunListQuery, Query()],
    service: Annotated[RunReadService, Depends(get_run_reads)],
) -> RunPage:
    return service.list(query)


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


@router.get("/{run_id}/trace", response_model=CanonicalTrace)
def get_canonical_trace(
    run_id: UUID, service: Annotated[RunService, Depends(get_run_service)]
) -> CanonicalTrace:
    return service.canonical_trace(run_id)


@router.get(
    "/{run_id}/original-trace",
    response_class=Response,
    responses={200: {"content": {"application/json": {"schema": {"type": "object"}}}}},
)
def get_original_trace(
    run_id: UUID, service: Annotated[RunService, Depends(get_run_service)]
) -> Response:
    record = service.get(run_id)
    return Response(
        content=record.original_trace,
        media_type="application/json",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
