import ast
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect, select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.exceptions import (
    CompilationInputError,
    CompilationPersistenceError,
    GeneratedSyntaxError,
    RegressionNotSynthesizableError,
    UnsupportedSpecVersionError,
)
from app.core.run_status import RunStatus
from app.db.models import (
    Attribution,
    AttributionEvidence,
    GeneratedTest,
    RegressionSpecOperation,
    Run,
    TraceStep,
)
from app.main import create_app
from app.services.attributions import AttributionStore
from app.services.generated_tests import GeneratedTestService
from app.services.pytest_compiler import PytestCompiler
from app.services.regression_input import load_evidence
from app.services.regression_validation import validate_regression
from app.services.runs import RunService
from tests.fixtures.replayable import calculator_spec, calculator_trace

pytestmark = pytest.mark.integration


@pytest.fixture
def compilation_setup(postgres, monkeypatch):
    _, sessions, _ = postgres
    trace, spec = calculator_trace(), calculator_spec()
    now = datetime.now(UTC)
    with sessions.begin() as session:
        run = Run(
            source=trace.source,
            original_trace=trace.model_dump_json().encode(),
            failure_signal="developer_report",
            trace_content_type="application/json",
            status=RunStatus.NORMALIZED,
            task=trace.task,
        )
        session.add(run)
        session.flush()
        for step in trace.steps:
            session.add(TraceStep(run_id=run.id, **step.model_dump()))
        session.flush()
        attr = Attribution(
            run_id=run.id,
            status="completed",
            decision="attributed",
            responsible_agent="calculator",
            decisive_sequence=1,
            explanation=spec.failure_summary,
            confidence="high",
            provider="offline_fixture",
            model="none",
            reasoning_effort="medium",
            prompt_version="attribution-v1",
            completed_at=now,
            context_manifest={"visible_sequences": [0, 1], "truncated_sequences": []},
        )
        session.add(attr)
        session.flush()
        session.add(
            AttributionEvidence(
                attribution_id=attr.id,
                position=0,
                start_sequence=0,
                end_sequence=1,
                reason="Setup and wrong sum",
                contains_truncated=False,
            )
        )
        attr_id, run_id = attr.id, run.id
    attributions = AttributionStore(sessions)
    resolved = validate_regression(
        spec, load_evidence(attributions.get(attr_id), trace), max_scenario_bytes=16000
    )
    assert resolved is not None
    with sessions.begin() as session:
        parent = RegressionSpecOperation(
            attribution_id=attr_id,
            status="completed",
            disposition="synthesizable",
            version=spec.version,
            title=spec.title,
            target_agent=spec.target_agent,
            spec=spec.model_dump(mode="json"),
            scenario_snapshot=resolved.model_dump(mode="json"),
            provider="offline_fixture",
            model="none",
            reasoning_effort="medium",
            prompt_version=spec.version,
            completed_at=now,
        )
        session.add(parent)
        session.flush()
        parent_id = parent.id
    settings = get_settings()
    runs = RunService(sessions, settings.max_request_body_bytes)
    service = GeneratedTestService(
        sessions,
        PytestCompiler(max_source_bytes=262144, max_scenario_bytes=16000),
        runs,
        attributions,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Phase 6 attempted paid inference or generated execution")

    monkeypatch.setattr("app.integrations.llm.openai.OpenAILLMClient.generate", forbidden)
    monkeypatch.setattr("app.integrations.llm.openai.OpenAILLMClient.attribute", forbidden)
    monkeypatch.setattr("tests.fixtures.replayable.ControlledCalculatorHarness.invoke", forbidden)
    return parent_id, run_id, service


def test_realistic_spec_compiles_and_retains_original_trace_and_scenario(compilation_setup):
    parent_id, run_id, service = compilation_setup
    original = service.runs.get(run_id).original_trace
    artifact, created = service.compile(parent_id, "compile-test")
    assert created and artifact.status == "completed" and artifact.framework == "pytest"
    assert artifact.source_digest == hashlib.sha256(artifact.source_code.encode()).hexdigest()
    assert ast.parse(artifact.source_code).body[2].name == calculator_spec().test_name
    assert (
        artifact.harness_identifier == "traceguard.agent-invocation"
        and artifact.harness_version == "1"
    )
    assert service.get(artifact.id) == artifact
    assert service.runs.get(run_id).original_trace == original
    assert service.compile(parent_id, "repeat") == (artifact, False)
    with service.sessions() as session:
        assert len(session.scalars(select(GeneratedTest)).all()) == 1


def test_api_201_repeat_200_get_and_safe_unknown_errors(compilation_setup):
    parent_id, _, service = compilation_setup
    with TestClient(create_app(get_settings())) as client:
        client.app.state.generated_tests = service
        path = f"/api/v1/regression-specs/{parent_id}/generated-tests"
        response = client.post(path)
        assert response.status_code == 201
        assert client.get(response.headers["location"]).json() == response.json()
        repeat = client.post(path)
        assert repeat.status_code == 200 and repeat.json() == response.json()
        assert response.json()["source_code"] and response.json()["source_digest"]
        unknown = client.get(f"/api/v1/generated-tests/{uuid4()}")
        assert (
            unknown.status_code == 404
            and unknown.json()["error"]["code"] == "GENERATED_TEST_NOT_FOUND"
        )
        assert unknown.json()["error"]["request_id"]
        assert client.post(f"/api/v1/regression-specs/{uuid4()}/generated-tests").status_code == 404


def test_concurrent_posts_compile_once_with_one_artifact(compilation_setup, monkeypatch):
    parent_id, _, service = compilation_setup
    calls = []
    original = service.compiler.compile

    def counted(record):
        calls.append(record.id)
        return original(record)

    monkeypatch.setattr(service.compiler, "compile", counted)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.compile(parent_id, "concurrent"), range(2)))
    assert results[0][0] == results[1][0]
    assert sorted(created for _, created in results) == [False, True]
    assert calls == [parent_id]


@pytest.mark.parametrize("mode", ["review", "pending", "failed"])
def test_noncompilable_lifecycle_never_creates_an_artifact(compilation_setup, mode):
    parent_id, _, service = compilation_setup
    with service.sessions.begin() as session:
        row = session.get(RegressionSpecOperation, parent_id)
        if mode == "review":
            row.disposition = "needs_review"
            row.review_reason = "Input cannot be replayed"
            row.scenario_snapshot = None
            row.spec = (
                calculator_spec()
                .model_copy(
                    update={
                        "disposition": "needs_review",
                        "scenario": None,
                        "assertions": (),
                        "review_reason": row.review_reason,
                    }
                )
                .model_dump(mode="json")
            )
        else:
            row.status = mode
            row.disposition = row.version = row.title = row.target_agent = None
            row.spec = row.scenario_snapshot = None
            row.completed_at = None
            if mode == "failed":
                row.failure_code = "LLM_OUTPUT_INVALID"
                row.failed_at = datetime.now(UTC)
    with pytest.raises(RegressionNotSynthesizableError):
        service.compile(parent_id, "invalid")
    with service.sessions() as session:
        assert session.scalar(select(GeneratedTest)) is None
    with TestClient(create_app(get_settings())) as client:
        client.app.state.generated_tests = service
        assert (
            client.post(f"/api/v1/regression-specs/{parent_id}/generated-tests").status_code == 409
        )


@pytest.mark.parametrize(
    "mode,error,code",
    [
        ("version", UnsupportedSpecVersionError, "REGRESSION_VERSION_UNSUPPORTED"),
        ("assertion", CompilationInputError, "COMPILATION_ASSERTION_UNSUPPORTED"),
        ("snapshot", CompilationInputError, "COMPILATION_SCENARIO_INVALID"),
        ("missing_stimulus", CompilationInputError, "COMPILATION_SCENARIO_INVALID"),
    ],
)
def test_corrupt_ir_or_snapshot_persists_safe_failure(compilation_setup, mode, error, code):
    parent_id, _, service = compilation_setup
    with service.sessions.begin() as session:
        row = session.get(RegressionSpecOperation, parent_id)
        if mode == "snapshot":
            row.scenario_snapshot = {
                "stimulus": {"left": 9, "right": 2},
                "preconditions": [{"operation": "add"}],
            }
        elif mode == "missing_stimulus":
            row.scenario_snapshot = {"stimulus": None, "preconditions": [{"operation": "add"}]}
        else:
            value = calculator_spec().model_dump(mode="json")
            if mode == "version":
                value["version"] = "unsupported-v9"
            else:
                value["assertions"][0]["kind"] = "python"
            row.spec = value
    with pytest.raises(error) as caught:
        service.compile(parent_id, "bad-data")
    artifact = service.get(caught.value.generated_test_id)
    assert artifact.status == "failed" and artifact.failure_code == code
    assert (
        artifact.source_code is None
        and artifact.source_digest is None
        and artifact.completed_at is None
    )
    assert service.compile(parent_id, "repeat") == (artifact, False)


def test_static_compiler_failure_is_committed_and_exposed_safely(compilation_setup, monkeypatch):
    parent_id, _, service = compilation_setup

    def invalid_source(record):
        raise GeneratedSyntaxError

    monkeypatch.setattr(service.compiler, "compile", invalid_source)
    with TestClient(create_app(get_settings())) as client:
        client.app.state.generated_tests = service
        response = client.post(f"/api/v1/regression-specs/{parent_id}/generated-tests")
        assert response.status_code == 500
        error = response.json()["error"]
        assert error["code"] == "GENERATED_SYNTAX_INVALID" and error["generated_test_id"]
        artifact = client.get(f"/api/v1/generated-tests/{error['generated_test_id']}").json()
        assert artifact["status"] == "failed" and artifact["source_code"] is None
        assert "Traceback" not in response.text


def test_database_failure_rolls_back_artifact_atomically(compilation_setup, postgres):
    parent_id, _, service = compilation_setup
    engine, _, _ = postgres

    def reject_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO generated_tests"):
            raise IntegrityError("private SQL", {}, Exception("secret details"))

    event.listen(engine, "after_cursor_execute", reject_insert)
    try:
        with pytest.raises(CompilationPersistenceError):
            service.compile(parent_id, "db-failure")
    finally:
        event.remove(engine, "after_cursor_execute", reject_insert)
    with service.sessions() as session:
        assert session.scalar(select(GeneratedTest)) is None
        assert session.get(RegressionSpecOperation, parent_id).status == "completed"
    assert service.compile(parent_id, "recover")[0].status == "completed"


def test_migration_constraints_foreign_keys_uniqueness_and_downgrade(compilation_setup, postgres):
    parent_id, run_id, service = compilation_setup
    engine, sessions, config = postgres
    command.check(config)
    artifact, _ = service.compile(parent_id, "migration")
    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.add(GeneratedTest(**artifact.model_dump(exclude={"id"})))
    with pytest.raises(IntegrityError), sessions.begin() as session:
        row = session.get(GeneratedTest, artifact.id)
        row.source_digest = "invalid"
    with pytest.raises(IntegrityError), sessions.begin() as session:
        values = artifact.model_dump(exclude={"id"})
        values["regression_spec_id"] = uuid4()
        session.add(GeneratedTest(**values))
    inspector = inspect(engine)
    assert inspector.get_foreign_keys("generated_tests")[0]["referred_table"] == "regression_specs"
    assert "uq_generated_tests_compilation" in {
        c["name"] for c in inspector.get_unique_constraints("generated_tests")
    }
    assert "ix_generated_tests_status_created_at" in {
        i["name"] for i in inspector.get_indexes("generated_tests")
    }
    original = service.runs.get(run_id).original_trace
    command.downgrade(config, "0004")
    assert "generated_tests" not in inspect(engine).get_table_names()
    assert service.runs.get(run_id).original_trace == original
    with sessions() as session:
        # Query only columns present at the historical revision after downgrade.
        assert (
            session.scalar(
                select(RegressionSpecOperation.status).where(
                    RegressionSpecOperation.id == parent_id
                )
            )
            == "completed"
        )
    command.upgrade(config, "head")
    command.check(config)
