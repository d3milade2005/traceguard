import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.exceptions import (
    GeneratedSyntaxError,
    LLMTransientError,
    ProcessingRetryError,
    QueuePublishError,
)
from app.db.models import Analysis, Attribution, GeneratedTest, RegressionSpecOperation
from app.integrations.execution.controlled_system import execute_calculator_system
from app.integrations.execution.docker_runner import DockerValidationRunner
from app.integrations.traces.sources import trace_adapters
from app.main import create_app
from app.schemas.attributions import AttributionDecision, EvidenceReference, LLMResult
from app.schemas.regression_specs import RegressionResult
from app.services.analyses import AnalysisService
from app.services.attribution import AttributionService
from app.services.attributions import AttributionStore
from app.services.context import ContextManager
from app.services.generated_tests import GeneratedTestService
from app.services.normalization import NormalizationService
from app.services.pytest_compiler import PytestCompiler
from app.services.regression import RegressionService
from app.services.regression_specs import RegressionStore
from app.services.runs import RunService
from app.services.submission import SubmissionService
from app.services.validations import ValidationService
from tests.fixtures.replayable import calculator_spec
from tests.integration.test_validations import FakeRunner

pytestmark = pytest.mark.integration


class Publisher:
    def __init__(self):
        self.calls = []

    def publish(self, identifier, request_id):
        self.calls.append(identifier)


@pytest.fixture
def pipeline(postgres):
    _, sessions, _ = postgres
    settings = get_settings()
    runs = RunService(sessions, settings.max_request_body_bytes)
    raw = execute_calculator_system().model_dump_json(indent=2).encode()
    run = SubmissionService(runs, Publisher(), trace_adapters()).submit(
        "controlled.calculator", raw, "pipeline-test"
    )
    NormalizationService(
        sessions,
        trace_adapters(),
        lease_seconds=60,
        max_trace_bytes=settings.max_request_body_bytes,
    ).process(run.id)
    stores = AttributionStore(sessions), RegressionStore(sessions)
    compiler = GeneratedTestService(
        sessions, PytestCompiler(max_source_bytes=262144, max_scenario_bytes=16000), runs, stores[0]
    )
    runner = FakeRunner()
    validation = ValidationService(sessions, settings, runner)
    publishers = [Publisher() for _ in range(4)]
    service = AnalysisService(sessions, *stores, compiler, validation, *publishers[:3])
    fake = Mock()
    fake.attribute.return_value = LLMResult(
        attribution=AttributionDecision(
            decision="attributed",
            responsible_agent="calculator",
            decisive_sequence=1,
            failure_category="arithmetic",
            explanation="The calculator returns 5 instead of 4.",
            confidence="high",
            evidence=(
                EvidenceReference(
                    start_sequence=0,
                    end_sequence=2,
                    reason="Setup, calculation, and downstream result",
                ),
            ),
            abstention_reason=None,
        )
    )
    fake.generate.return_value = RegressionResult(spec=calculator_spec())
    attr_worker = AttributionService(
        sessions, runs, ContextManager(settings.analysis_context), fake, lease_seconds=60
    )
    spec_worker = RegressionService(
        sessions,
        runs,
        stores[0],
        fake,
        lease_seconds=60,
        max_input_bytes=48000,
        max_scenario_bytes=16000,
    )
    service.submit(
        run.id, "pipeline-test", publishers[3], model="gpt-5.6-luna", reasoning_effort="medium"
    )
    return run.id, service, attr_worker, spec_worker, fake, runner, publishers, raw


def drive(pipeline):
    run, service, attr_worker, spec_worker, *_ = pipeline
    for _ in range(8):
        pending = service.advance(run)
        record = service.get(run)
        if not pending:
            return record
        if record.attribution_id:
            attr_worker.process(record.attribution_id)
        if record.regression_spec_id:
            spec_worker.process(record.regression_spec_id)
        if record.validation_id:
            service.validations.process(record.validation_id, final_attempt=True)
    raise AssertionError("Pipeline did not terminate")


def test_pipeline_real_services_persist_valid_and_duplicate_delivery_is_free(pipeline, postgres):
    run, service, _, _, fake, runner, publishers, raw = pipeline
    result = drive(pipeline)
    assert result.status == "completed" and result.stop_reason == "valid"
    assert service.validations.get(result.validation_id).verdict == "valid"
    assert fake.attribute.call_count == fake.generate.call_count == 1
    assert runner.calls[0][1] == runner.calls[1][1]
    assert service.compiler.runs.get(run).original_trace == raw
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.advance(run), range(4)))
    assert results == [False] * 4
    repeated, created = service.submit(
        run, "repeat", publishers[3], model="other", reasoning_effort="low"
    )
    assert not created and repeated == result
    assert fake.attribute.call_count == fake.generate.call_count == 1
    command.check(postgres[2])


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("no_reproduction", "not_reproduced"),
        ("both_fail", "not_discriminating"),
        ("timeout", "inconclusive"),
    ],
)
def test_validation_stops_for_review(pipeline, mode, reason):
    pipeline[5].mode = mode
    result = drive(pipeline)
    assert result.status == "review_required" and result.stop_reason == reason


def test_abstention_does_not_request_regression(pipeline):
    fake = pipeline[4]
    fake.attribute.return_value = LLMResult(
        attribution=AttributionDecision(
            decision="abstain",
            responsible_agent=None,
            decisive_sequence=None,
            failure_category=None,
            explanation="Ambiguous",
            confidence="low",
            evidence=(),
            abstention_reason="Insufficient evidence",
        )
    )
    result = drive(pipeline)
    assert result.stop_reason == "attribution_abstained"
    assert result.regression_spec_id is None
    fake.generate.assert_not_called()


def test_needs_review_never_compiles(pipeline):
    pipeline[4].generate.return_value = RegressionResult(
        spec=calculator_spec().model_copy(
            update={
                "disposition": "needs_review",
                "scenario": None,
                "assertions": (),
                "review_reason": "Uncertain setup",
            }
        )
    )
    result = drive(pipeline)
    assert result.stop_reason == "regression_needs_review" and result.generated_test_id is None


def test_compilation_failure_preserves_artifact_and_stops(pipeline, monkeypatch):
    def fail(record):
        raise GeneratedSyntaxError

    monkeypatch.setattr(pipeline[1].compiler.compiler, "compile", fail)
    result = drive(pipeline)
    assert result.status == "failed" and result.stop_reason == "GENERATED_SYNTAX_INVALID"
    assert result.generated_test_id is not None and result.validation_id is None


def test_incompatible_recorded_setup_stops_for_review_without_validation(pipeline):
    spec = calculator_spec()
    setup = spec.scenario.preconditions[0].model_copy(update={"path": ("operation",)})
    scenario = spec.scenario.model_copy(update={"preconditions": (setup,)})
    pipeline[4].generate.return_value = RegressionResult(
        spec=spec.model_copy(update={"scenario": scenario})
    )
    result = drive(pipeline)
    assert result.status == "review_required"
    assert result.stop_reason == "REPLAY_CONTRACT_INCOMPATIBLE"
    assert result.generated_test_id is not None and result.validation_id is None
    assert not pipeline[5].calls


def test_single_attempt_even_after_transient_failure_or_duplicate(pipeline):
    run, service, worker, _, fake, *_ = pipeline
    fake.attribute.side_effect = LLMTransientError
    service.advance(run)
    attribution = service.get(run).attribution_id
    with pytest.raises(ProcessingRetryError):
        worker.process(attribution)
    worker.process(attribution)
    assert fake.attribute.call_count == 1
    assert not service.advance(run)
    assert service.get(run).status == "failed"


def test_expired_claim_never_repeats_paid_attempt(pipeline):
    run, service, worker, _, fake, *_ = pipeline
    service.advance(run)
    attribution = service.get(run).attribution_id
    assert worker.claim(attribution) is not None
    with service.sessions.begin() as session:
        session.get(Attribution, attribution).claim_expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
    worker.process(attribution)
    fake.attribute.assert_not_called()
    assert service.attributions.get(attribution).failure_code == "ATTRIBUTION_ATTEMPTS_EXHAUSTED"


def test_deadline_and_api_idempotency(pipeline, monkeypatch):
    run, service, *rest = pipeline
    monkeypatch.setattr("app.api.v1.analyses.CeleryAnalysisPublisher", lambda *args: Publisher())
    with TestClient(create_app(get_settings())) as client:
        client.app.state.analyses = service
        path = f"/api/v1/runs/{run}/analysis"
        assert client.post(path).status_code == 200
        assert client.get(path).json()["status"] == "active"
        assert client.get(f"/api/v1/runs/{uuid4()}/analysis").status_code == 404
        assert client.post(f"/api/v1/runs/{uuid4()}/analysis").status_code == 404
        with service.sessions.begin() as session:
            row = session.scalar(select(Analysis).where(Analysis.run_id == run))
            row.deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        result = client.get(path).json()
        assert result["status"] == "failed"
        assert result["stop_reason"] == "ANALYSIS_DEADLINE_EXCEEDED"
    assert not service.advance(run)
    with service.sessions() as session:
        assert session.scalar(select(Attribution)) is None
        assert session.scalar(select(RegressionSpecOperation)) is None
        assert session.scalar(select(GeneratedTest)) is None


def test_concurrent_active_delivery_creates_one_attribution(pipeline):
    run, service, _, _, fake, _, publishers, _ = pipeline
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(pool.map(lambda _: service.advance(run), range(2)))
    assert len(publishers[0].calls) == 1
    fake.attribute.assert_not_called()
    assert drive(pipeline).stop_reason == "valid"


def test_regression_transient_failure_consumes_single_attempt(pipeline):
    run, service, attr_worker, spec_worker, fake, *_ = pipeline
    service.advance(run)
    attr_worker.process(service.get(run).attribution_id)
    service.advance(run)
    service.advance(run)
    identifier = service.get(run).regression_spec_id
    fake.generate.side_effect = LLMTransientError
    with pytest.raises(ProcessingRetryError):
        spec_worker.process(identifier)
    spec_worker.process(identifier)
    assert fake.generate.call_count == 1
    assert not service.advance(run)
    assert service.get(run).status == "failed"


def test_submission_commit_precedes_publication_and_queue_error_is_durable(pipeline):
    run, service, *_ = pipeline

    class FailingPublisher:
        def publish(self, identifier, request_id):
            assert service.get(identifier).status == "active"
            raise QueuePublishError(run_id=identifier)

    with pytest.raises(QueuePublishError):
        service.submit(
            run,
            "queue-failure",
            FailingPublisher(),
            model="gpt-5.6-luna",
            reasoning_effort="medium",
        )
    assert service.get(run).stop_reason == "QUEUE_PUBLICATION_FAILED"
    assert not service.advance(run)


def test_stage_queue_failure_keeps_link_to_persisted_stage(pipeline):
    run, service, *_ = pipeline

    class FailingPublisher:
        def publish(self, identifier, request_id):
            raise QueuePublishError

    service.attribution_publisher = FailingPublisher()
    assert not service.advance(run)
    result = service.get(run)
    assert result.status == "failed" and result.attribution_id is not None
    assert service.attributions.get(result.attribution_id).status == "failed"


def test_validation_infrastructure_failure_stops_analysis(pipeline):
    run, service, attr_worker, spec_worker, _, runner, *_ = pipeline
    runner.mode = "unavailable"
    for _ in range(7):
        service.advance(run)
        record = service.get(run)
        if record.validation_id:
            with pytest.raises(ProcessingRetryError):
                service.validations.process(record.validation_id, final_attempt=True)
            break
        if record.attribution_id:
            attr_worker.process(record.attribution_id)
        if record.regression_spec_id:
            spec_worker.process(record.regression_spec_id)
    assert not service.advance(run)
    assert service.get(run).stop_reason == "VALIDATION_INFRASTRUCTURE_FAILED"


@pytest.mark.sandbox
def test_coordinated_controlled_execution_with_real_docker_and_fake_inference(pipeline):
    if os.environ.get("RUN_SANDBOX_TESTS") != "true":
        pytest.skip("Set RUN_SANDBOX_TESTS=true for real isolated validation")
    pipeline[1].validations.runner = DockerValidationRunner(get_settings())
    result = drive(pipeline)
    assert result.status == "completed" and result.stop_reason == "valid"
    validation = pipeline[1].validations.get(result.validation_id)
    assert validation.faulty_outcome == "assertion_failed"
    assert validation.corrected_outcome == "passed"
    assert pipeline[4].attribute.call_count == pipeline[4].generate.call_count == 1
