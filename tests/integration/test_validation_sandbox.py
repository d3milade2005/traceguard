import os
from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.integrations.execution.contracts import ExecutionBundle
from app.integrations.execution.docker_runner import DockerValidationRunner
from app.schemas.validations import SandboxLimits
from app.services.validations import ValidationService
from tests.integration.test_generated_tests import compilation_setup as compilation_setup
from tests.integration.test_validations import Publisher

pytestmark = [pytest.mark.integration, pytest.mark.sandbox]


@pytest.fixture
def sandbox():
    if os.environ.get("RUN_SANDBOX_TESTS") != "true":
        pytest.skip("Set RUN_SANDBOX_TESTS=true with the pinned local image and Linux Docker")
    runner = DockerValidationRunner(get_settings())
    runtime = runner.prepare(runner.settings.validation_image)
    yield runner, runtime
    runner.cleanup_expired()
    remaining = runner._command(
        [
            "ps",
            "--all",
            "--filter",
            "label=org.traceguard.managed=regression-sandbox-v1",
            "--format",
            "{{.ID}}",
        ]
    )
    assert remaining.code == 0 and not remaining.stdout.strip()


def execute_probe(sandbox, body, timeout=None):
    runner, runtime = sandbox
    if timeout is not None:
        runner = DockerValidationRunner(
            runner.settings.model_copy(update={"validation_timeout_seconds": timeout})
        )
        runtime = runtime.model_copy(
            update={"limits": SandboxLimits.from_settings(runner.settings)}
        )
    # Developer-owned security probes bypass compilation solely to exercise the boundary.
    # Their Python runs only in Docker, never in this test process.
    source = "def test_probe():\n" + "\n".join("    " + line for line in body.splitlines()) + "\n"
    return runner.execute(
        ExecutionBundle(source, "test_probe"), runtime, "faulty", uuid4(), uuid4()
    )


def test_real_faulty_fail_corrected_pass_is_persisted(sandbox, compilation_setup):
    parent, _, compiler = compilation_setup
    artifact, _ = compiler.compile(parent, "controlled-e2e")
    runner, _ = sandbox
    service = ValidationService(compiler.sessions, get_settings(), runner)
    accepted, _ = service.submit(artifact.id, "controlled-e2e", Publisher(service))
    service.process(accepted.id)
    record = service.get(accepted.id)
    assert record.status == "completed" and record.verdict == "valid"
    assert record.faulty_execution.outcome == "assertion_failed"
    assert record.corrected_execution.outcome == "passed"
    assert record.runtime_identity.image_id.startswith("sha256:")
    assert record.source_digest == artifact.source_digest


def test_nonroot_readonly_tmp_network_and_no_host_access(sandbox):
    probe = """import os
from pathlib import Path
import socket
assert os.geteuid() == 10001
status = Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\t1' in status
assert 'Seccomp:\t2' in status
assert 'CapEff:\t0000000000000000' in status
assert not Path('/var/run/docker.sock').exists()
assert not Path('/app/.env').exists()
assert not Path('/app/AGENTS.md').exists()
assert not Path('/app/.git').exists()
assert not Path('/opt/runtime/app/main.py').exists()
assert not {'OPENAI_API_KEY','DATABASE_URL','RABBITMQ_URL'} & set(os.environ)
try:
    Path('/opt/runtime/escape').write_text('x')
except OSError:
    pass
else:
    raise RuntimeError('Root filesystem was writable')
Path('/tmp/allowed').write_text('ok')
s = socket.socket()
s.settimeout(0.2)
try:
    s.connect(('1.1.1.1',443))
except OSError:
    pass
else:
    raise RuntimeError('External network was reachable')
finally:
    s.close()"""
    assert execute_probe(sandbox, probe).outcome == "passed"


@pytest.mark.parametrize(
    "body,diagnostic",
    [
        ("raise RuntimeError('harness secret')", "EXECUTION_ERROR"),
        (
            "from app import regression_runtime as r\nr.require_observation({'output':4})",
            "EXECUTION_ERROR",
        ),
        (
            "raise AssertionError('ordinary assertion is not regression reproduction')",
            "EXECUTION_ERROR",
        ),
        ("print('x' * 100000)", "OUTPUT_LIMIT"),
        ("import time\ntime.sleep(30)", "TIMEOUT"),
    ],
)
def test_runtime_errors_timeout_and_output_limits_are_not_reproduction(sandbox, body, diagnostic):
    result = execute_probe(sandbox, body, timeout=2 if diagnostic == "TIMEOUT" else None)
    assert result.diagnostic == diagnostic and result.outcome != "assertion_failed"
    assert "secret" not in result.model_dump_json()


def test_tmpfs_limit_is_enforced(sandbox):
    body = """from pathlib import Path
import os
import errno
try:
    with Path('/tmp/large').open('wb') as f:
        os.posix_fallocate(f.fileno(), 0, 80 * 1048576)
except OSError as error:
    assert error.errno == errno.ENOSPC
else:
    raise RuntimeError('Temporary storage was unbounded')
finally:
    Path('/tmp/large').unlink(missing_ok=True)"""
    assert execute_probe(sandbox, body).outcome == "passed"


def test_memory_limit_termination_is_not_reproduction(sandbox):
    body = "blocks=[]\nwhile True:\n    blocks.append(bytearray(1048576))"
    result = execute_probe(sandbox, body)
    assert result.outcome == "resource_limit" and result.resource_limited


def test_process_limit_prevents_unbounded_children(sandbox):
    body = """import subprocess
children=[]
try:
    for _ in range(80):
        children.append(subprocess.Popen(['python','-c','import time; time.sleep(20)']))
except OSError:
    pass
else:
    raise RuntimeError('Process count was unbounded')
finally:
    for child in children:
        child.terminate()
    for child in children:
        child.wait()"""
    assert execute_probe(sandbox, body).outcome == "passed"
