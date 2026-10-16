from celery import Celery
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.integrations.task_queue import (
    CeleryAttributionPublisher,
    CeleryRegressionPublisher,
    CeleryValidationPublisher,
)
from app.services.analyses import AnalysisService
from app.services.attributions import AttributionStore
from app.services.generated_tests import GeneratedTestService
from app.services.pytest_compiler import PytestCompiler
from app.services.regression_specs import RegressionStore
from app.services.runs import RunService
from app.services.validations import ValidationService


def create_analysis_service(
    sessions: sessionmaker[Session], settings: Settings, queue: Celery
) -> AnalysisService:
    attributions = AttributionStore(sessions)
    return AnalysisService(
        sessions,
        attributions,
        RegressionStore(sessions),
        GeneratedTestService(
            sessions,
            PytestCompiler(
                max_source_bytes=settings.generated_test_max_source_bytes,
                max_scenario_bytes=settings.regression_max_scenario_bytes,
            ),
            RunService(sessions, settings.max_request_body_bytes),
            attributions,
        ),
        ValidationService(sessions, settings),
        CeleryAttributionPublisher(queue, settings.dependency_timeout_seconds),
        CeleryRegressionPublisher(queue, settings.dependency_timeout_seconds),
        CeleryValidationPublisher(queue, settings.dependency_timeout_seconds),
    )
