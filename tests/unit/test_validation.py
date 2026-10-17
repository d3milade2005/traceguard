import hashlib
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.exceptions import SandboxPolicyError, ValidationArtifactError
from app.integrations.execution.docker_runner import CommandResult, DockerValidationRunner
from app.integrations.execution.registry import approved_harness, bundle_digest
from app.integrations.execution.results import parse_result
from app.schemas.validations import ExecutionResult, RuntimeIdentity, SandboxLimits, classify
from app.services.pytest_compiler import PytestCompiler
from app.services.validation_artifact import verify_artifact
from tests.fixtures.replayable import calculator_record


def result(variant, outcome):
    return ExecutionResult(
        variant=variant,
        outcome=outcome,
        duration_ms=5,
        container_id="a" * 64,
        exit_code=0 if outcome == "passed" else 1,
        passed=int(outcome == "passed"),
        failed=int(outcome == "assertion_failed"),
        timed_out=outcome == "timeout",
        resource_limited=outcome == "resource_limit",
        diagnostic="EXECUTION_ERROR",
    )


@pytest.mark.parametrize(
    "faulty,corrected,verdict",
    [
        ("assertion_failed", "passed", "valid"),
        ("passed", "passed", "not_reproduced"),
        ("passed", "assertion_failed", "not_reproduced"),
        ("assertion_failed", "assertion_failed", "not_discriminating"),
        ("execution_error", "passed", "inconclusive"),
        ("timeout", "passed", "inconclusive"),
        ("assertion_failed", "execution_error", "inconclusive"),
        ("passed", "resource_limit", "inconclusive"),
    ],
)
def test_validation_verdicts(faulty, corrected, verdict):
    assert classify(result("faulty", faulty), result("corrected", corrected)) == verdict


@pytest.mark.parametrize(
    "xml,code,outcome",
    [
        ('<testsuites><testsuite><testcase name="test_x"/></testsuite></testsuites>', 0, "passed"),
        (
            "<testsuites><testsuite><testcase "
            'name="test_x"><failure/></testcase></testsuite></testsuites>',
            1,
            "execution_error",
        ),
        (
            '<testsuites><testsuite><testcase name="test_x"><properties><property '
            'name="traceguard_assertion" '
            'value="true"/></properties><failure/></testcase></testsuite></testsuites>',
            1,
            "assertion_failed",
        ),
        (
            "<testsuites><testsuite><testcase "
            'name="test_x"><error/></testcase></testsuite></testsuites>',
            1,
            "execution_error",
        ),
        (
            '<testsuites><testsuite><testcase name="other"/></testsuite></testsuites>',
            0,
            "execution_error",
        ),
        (
            '<testsuites><testsuite><testcase name="test_x"/><testcase '
            'name="test_x"/></testsuite></testsuites>',
            0,
            "execution_error",
        ),
        ('<!DOCTYPE x [<!ENTITY x SYSTEM "file:///secret">]><x/>', 0, "execution_error"),
        ("<broken", 0, "execution_error"),
    ],
)
def test_bounded_junit_requires_one_case_and_assertion_provenance(xml, code, outcome):
    execution = parse_result(
        variant="faulty",
        container_id="a" * 64,
        duration_ms=1,
        exit_code=code,
        junit=xml,
        test_name="test_x",
        max_result_bytes=4096,
    )
    assert execution.outcome == outcome
    assert "secret" not in execution.model_dump_json()


@pytest.mark.parametrize(
    "changes,outcome",
    [
        ({"timed_out": True}, "timeout"),
        ({"oom": True}, "resource_limit"),
        ({"exit_code": 137}, "resource_limit"),
        ({"output_truncated": True}, "execution_error"),
        ({"junit": "x" * 5000}, "execution_error"),
    ],
)
def test_termination_and_output_limits_override_pytest(changes, outcome):
    values = dict(
        variant="faulty",
        container_id="a" * 64,
        duration_ms=1,
        exit_code=0,
        junit=None,
        test_name="test_x",
        max_result_bytes=4096,
    )
    values.update(changes)
    assert parse_result(**values).outcome == outcome


def test_registry_is_explicit_and_scenario_compatible():
    record = calculator_record()
    approved_harness("traceguard.agent-invocation", "1").check(
        record.spec, record.scenario_snapshot
    )
    for identifier, version in [("module.function", "1"), ("traceguard.agent-invocation", "2")]:
        with pytest.raises(ValidationArtifactError):
            approved_harness(identifier, version)
    with pytest.raises(ValidationArtifactError):
        approved_harness("traceguard.agent-invocation", "1").check(
            record.spec.model_copy(update={"target_agent": "WhoWhen"}), record.scenario_snapshot
        )
    assert len(bundle_digest()) == 64


def artifact_and_session():
    record = calculator_record()
    compiled = PytestCompiler(max_source_bytes=262144, max_scenario_bytes=16000).compile(record)
    artifact = SimpleNamespace(
        id=uuid4(),
        regression_spec_id=record.id,
        status="completed",
        **compiled.__dict__,
        failure_code=None,
        failed_at=None,
        completed_at=record.completed_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )
    session = Mock()
    session.get.return_value = record
    return artifact, session


def test_artifact_verified_without_regeneration_or_execution(settings, monkeypatch):
    artifact, session = artifact_and_session()
    monkeypatch.setattr(PytestCompiler, "compile", Mock(side_effect=AssertionError("No recompile")))
    accepted, bundle = verify_artifact(session, artifact, settings)
    assert bundle.source == artifact.source_code and accepted.id == artifact.id


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_digest", "0" * 64),
        ("compiler_version", "unknown"),
        ("harness_version", "99"),
        ("framework", "unittest"),
        ("version", "unknown"),
        ("status", "failed"),
        ("test_name", "test_other"),
    ],
)
def test_artifact_mismatches_fail_closed(settings, field, value):
    artifact, session = artifact_and_session()
    setattr(artifact, field, value)
    with pytest.raises(ValidationArtifactError):
        verify_artifact(session, artifact, settings)


@pytest.mark.parametrize("source", ["def invalid(:", "import os\n", "modified_fixture"])
def test_static_checks_reject_mutated_source_even_if_digest_is_replaced(settings, source):
    artifact, session = artifact_and_session()
    if source == "modified_fixture":
        source = artifact.source_code.replace('"left":2', '"left":3')
    artifact.source_code = source
    artifact.source_digest = hashlib.sha256(source.encode()).hexdigest()
    with pytest.raises(ValidationArtifactError):
        verify_artifact(session, artifact, settings)


def test_container_policy_is_fixed_and_only_trusted_configuration_contributes_options(settings):
    runner = DockerValidationRunner(settings)
    runtime = RuntimeIdentity(
        image_reference=settings.validation_image,
        image_id="sha256:" + "a" * 64,
        bundle_digest=bundle_digest(),
        limits=SandboxLimits.from_settings(settings),
    )
    args = runner.create_args(runtime, "faulty", uuid4(), uuid4(), "traceguard-validation-unit")
    for flag, value in [
        ("--network", "none"),
        ("--user", "10001:10001"),
        ("--cap-drop", "ALL"),
        ("--log-driver", "none"),
        ("--ipc", "none"),
    ]:
        assert args[args.index(flag) + 1] == value
    assert "--read-only" in args and "--privileged" not in args
    assert "--mount" not in args and "--volume" not in args
    assert "unconfined" not in str(args) and "docker.sock" not in str(args)
    security_options = [args[i + 1] for i, arg in enumerate(args) if arg == "--security-opt"]
    assert security_options == ["no-new-privileges:true", "seccomp=builtin"]
    assert args[-1] == runtime.image_id


@pytest.mark.parametrize(
    "options,accepted",
    [
        (["name=seccomp,profile=builtin"], True),
        (["name=seccomp,profile=unconfined"], True),
        (["name=cgroupns"], False),
        (["name=not-seccomp"], False),
    ],
)
def test_preflight_requires_seccomp_capability_and_container_selects_filter(
    settings, monkeypatch, options, accepted
):
    runner = DockerValidationRunner(settings)
    responses = iter(
        [
            {
                "OSType": "linux",
                "MemoryLimit": True,
                "PidsLimit": True,
                "CpuCfsQuota": True,
                "SecurityOptions": options,
            },
            [
                {
                    "Id": "sha256:" + "a" * 64,
                    "Os": "linux",
                    "Config": {
                        "User": "10001:10001",
                        "Entrypoint": ["python", "-I", "/opt/runner/entrypoint.py"],
                        "Labels": {
                            "org.traceguard.runtime": "regression-sandbox-v1",
                            "org.traceguard.bundle": bundle_digest(),
                        },
                    },
                }
            ],
        ]
    )
    monkeypatch.setattr(runner, "_json", lambda args: next(responses))
    if accepted:
        runtime = runner.prepare(settings.validation_image)
        args = runner.create_args(runtime, "faulty", uuid4(), uuid4(), "unit")
        assert "seccomp=builtin" in args
    else:
        with pytest.raises(SandboxPolicyError):
            runner.prepare(settings.validation_image)


@pytest.mark.parametrize(
    "image", ["latest", "runner:latest", "runner", "-bad", "runner:1 --privileged"]
)
def test_floating_or_unsafe_image_configuration_rejected(settings, image):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            database_url=settings.database_url,
            rabbitmq_url=settings.rabbitmq_url,
            validation_image=image,
        )


def test_cleanup_runs_even_on_container_startup_failure(settings, monkeypatch):
    runner = DockerValidationRunner(settings)
    removed = []
    monkeypatch.setattr(
        runner,
        "_command",
        lambda *args, **kwargs: CommandResult(1, b"", b"policy invalid", False, False),
    )
    monkeypatch.setattr(runner, "_remove", removed.append)
    from app.integrations.execution.contracts import ExecutionBundle

    runtime = RuntimeIdentity(
        image_reference=settings.validation_image,
        image_id="sha256:" + "a" * 64,
        bundle_digest=bundle_digest(),
        limits=SandboxLimits.from_settings(settings),
    )
    with pytest.raises(SandboxPolicyError):
        runner.execute(ExecutionBundle("data", "test_x"), runtime, "faulty", uuid4(), uuid4())
    assert len(removed) == 1 and removed[0].startswith("traceguard-validation-")


def test_validation_task_is_registered_only_in_dedicated_app(settings):
    from app.workers.factory import create_celery_app

    normal = create_celery_app(settings)
    dedicated = create_celery_app(settings, validation_worker=True)
    assert "traceguard.validate_regression" not in normal.tasks
    assert "traceguard.validate_regression" in dedicated.tasks
    assert {q.name for q in normal.conf.task_queues} == {"traceguard"}
    assert {q.name for q in dedicated.conf.task_queues} == {"traceguard.validation"}
    assert "traceguard.attribute_run" not in dedicated.tasks
    normal.close()
    dedicated.close()
