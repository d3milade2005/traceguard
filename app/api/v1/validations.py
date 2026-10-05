from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from app.api.dependencies import get_validation_publisher, get_validation_service
from app.integrations.task_queue import CeleryValidationPublisher
from app.schemas.validations import ValidationAccepted, ValidationRecord
from app.services.validations import ValidationService

router = APIRouter(tags=["validations"])


@router.post(
    "/generated-tests/{generated_test_id}/validations",
    status_code=202,
    response_model=ValidationAccepted,
    responses={200: {"model": ValidationAccepted}},
)
def submit_validation(
    generated_test_id: UUID,
    request: Request,
    response: Response,
    service: Annotated[ValidationService, Depends(get_validation_service)],
    publisher: Annotated[CeleryValidationPublisher, Depends(get_validation_publisher)],
) -> ValidationAccepted:
    result, created = service.submit(generated_test_id, request.state.request_id, publisher)
    response.status_code = 202 if created else 200
    response.headers["Location"] = str(
        request.url_for("get_validation", validation_id=str(result.id))
    )
    return result


@router.get("/validations/{validation_id}", response_model=ValidationRecord)
def get_validation(
    validation_id: UUID,
    service: Annotated[ValidationService, Depends(get_validation_service)],
) -> ValidationRecord:
    return service.get(validation_id)
