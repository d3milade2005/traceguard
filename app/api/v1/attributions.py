from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from app.api.dependencies import get_attribution_publisher, get_attribution_store
from app.integrations.task_queue import CeleryAttributionPublisher
from app.schemas.attributions import AttributionAccepted, AttributionRecord
from app.services.attributions import AttributionStore

router = APIRouter(tags=["attributions"])


@router.post(
    "/runs/{run_id}/attributions",
    status_code=202,
    response_model=AttributionAccepted,
    responses={200: {"model": AttributionAccepted}},
)
def request_attribution(
    run_id: UUID,
    request: Request,
    response: Response,
    store: Annotated[AttributionStore, Depends(get_attribution_store)],
    publisher: Annotated[CeleryAttributionPublisher, Depends(get_attribution_publisher)],
) -> AttributionAccepted:
    settings = request.app.state.settings
    result, created = store.submit(
        run_id,
        request.state.request_id,
        publisher,
        model=settings.openai_model,
        reasoning_effort=settings.openai_reasoning_effort,
    )
    response.status_code = 202 if created else 200
    response.headers["Location"] = str(
        request.url_for("get_attribution", attribution_id=str(result.id))
    )
    return result


@router.get("/attributions/{attribution_id}", response_model=AttributionRecord)
def get_attribution(
    attribution_id: UUID,
    store: Annotated[AttributionStore, Depends(get_attribution_store)],
) -> AttributionRecord:
    return store.get(attribution_id)
