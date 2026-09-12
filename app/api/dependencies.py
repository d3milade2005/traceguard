from typing import cast

from fastapi import Request

from app.services.readiness import ReadinessService


def get_readiness_service(request: Request) -> ReadinessService:
    return cast(ReadinessService, request.app.state.readiness)
