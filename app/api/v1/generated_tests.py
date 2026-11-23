from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from app.api.dependencies import get_generated_test_service
from app.schemas.generated_tests import GeneratedTestRecord
from app.services.generated_tests import GeneratedTestService

router = APIRouter(tags=["generated-tests"])


@router.post(
    "/regression-specs/{regression_spec_id}/generated-tests",
    status_code=201,
    response_model=GeneratedTestRecord,
    responses={200: {"model": GeneratedTestRecord}},
)
def compile_generated_test(
    regression_spec_id: UUID,
    request: Request,
    response: Response,
    service: Annotated[GeneratedTestService, Depends(get_generated_test_service)],
) -> GeneratedTestRecord:
    result, created = service.compile(regression_spec_id, request.state.request_id)
    response.status_code = 201 if created else 200
    response.headers["Location"] = str(
        request.url_for("get_generated_test", generated_test_id=str(result.id))
    )
    return result


@router.get("/generated-tests/{generated_test_id}", response_model=GeneratedTestRecord)
def get_generated_test(
    generated_test_id: UUID,
    service: Annotated[GeneratedTestService, Depends(get_generated_test_service)],
) -> GeneratedTestRecord:
    return service.get(generated_test_id)
