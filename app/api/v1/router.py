from fastapi import APIRouter, Depends

from app.api.dependencies import require_user
from app.api.v1 import validations
from app.api.v1.analyses import router as analyses_router
from app.api.v1.attributions import router as attributions_router
from app.api.v1.generated_tests import router as generated_tests_router
from app.api.v1.read_views import router as read_views_router
from app.api.v1.regression_specs import router as regression_router
from app.api.v1.runs import router as runs_router

router = APIRouter(dependencies=[Depends(require_user)])
router.include_router(read_views_router)
router.include_router(analyses_router)
router.include_router(validations.router)
router.include_router(runs_router)
router.include_router(attributions_router)
router.include_router(regression_router)
router.include_router(generated_tests_router)
