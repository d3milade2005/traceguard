from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from app.api.dependencies import get_regression_publisher, get_regression_store
from app.integrations.task_queue import CeleryRegressionPublisher
from app.schemas.regression_specs import RegressionAccepted, RegressionRecord
from app.services.regression_specs import RegressionStore

router = APIRouter(tags=["regression-specs"])


@router.post(
    "/attributions/{attribution_id}/regression-specs",
    status_code=202,
    response_model=RegressionAccepted,
    responses={200: {"model": RegressionAccepted}},
)
def request_regression_spec(
    attribution_id: UUID,
    request: Request,
    response: Response,
    store: Annotated[RegressionStore, Depends(get_regression_store)],
    publisher: Annotated[CeleryRegressionPublisher, Depends(get_regression_publisher)],
) -> RegressionAccepted:
    settings = request.app.state.settings
    result, created = store.submit(
        attribution_id,
        request.state.request_id,
        publisher,
        model=settings.openai_model,
        reasoning_effort=settings.openai_reasoning_effort,
    )
    response.status_code = 202 if created else 200
    response.headers["Location"] = str(
        request.url_for("get_regression_spec", regression_spec_id=str(result.id))
    )
    return result


@router.get("/regression-specs/{regression_spec_id}", response_model=RegressionRecord)
def get_regression_spec(
    regression_spec_id: UUID,
    store: Annotated[RegressionStore, Depends(get_regression_store)],
) -> RegressionRecord:
    return store.get(regression_spec_id)
