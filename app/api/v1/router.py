from fastapi import APIRouter

from app.api.v1 import validations
from app.api.v1.attributions import router as attributions_router
from app.api.v1.generated_tests import router as generated_tests_router
from app.api.v1.regression_specs import router as regression_router
from app.api.v1.runs import router as runs_router

router = APIRouter()
router.include_router(validations.router)
router.include_router(runs_router)
router.include_router(attributions_router)
router.include_router(regression_router)
router.include_router(generated_tests_router)
