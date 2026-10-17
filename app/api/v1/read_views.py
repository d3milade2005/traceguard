from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.dependencies import get_run_reads
from app.core.context import CONTEXT_POLICY_VERSION
from app.schemas.runs import DashboardSummary
from app.schemas.system import SystemInfo
from app.schemas.validations import VALIDATOR_VERSION
from app.services.attribution_input import PROMPT_VERSION as ATTRIBUTION_PROMPT_VERSION
from app.services.pytest_compiler import COMPILER_VERSION, SUPPORTED_SPEC_VERSION
from app.services.regression_input import PROMPT_VERSION as REGRESSION_PROMPT_VERSION
from app.services.run_reads import RunReadService

router = APIRouter(tags=["read-views"])


@router.get("/dashboard", response_model=DashboardSummary)
def dashboard(service: Annotated[RunReadService, Depends(get_run_reads)]) -> DashboardSummary:
    return service.dashboard()


@router.get("/system", response_model=SystemInfo)
def system(request: Request) -> SystemInfo:
    settings = request.app.state.settings
    return SystemInfo(
        environment=settings.app_env,
        model=settings.openai_model,
        reasoning_effort=settings.openai_reasoning_effort,
        attribution_prompt_version=ATTRIBUTION_PROMPT_VERSION,
        context_policy_version=CONTEXT_POLICY_VERSION,
        regression_spec_version=SUPPORTED_SPEC_VERSION,
        regression_prompt_version=REGRESSION_PROMPT_VERSION,
        compiler_version=COMPILER_VERSION,
        validator_version=VALIDATOR_VERSION,
    )
