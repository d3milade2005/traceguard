"""Narrow Docker CLI boundary; the client never runs or imports artifact Python."""

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from uuid import UUID, uuid4

from app.core.config import Settings
from app.core.exceptions import SandboxCleanupError, SandboxPolicyError, SandboxUnavailableError
from app.integrations.execution.contracts import ExecutionBundle
from app.integrations.execution.registry import bundle_digest
from app.integrations.execution.results import parse_result
from app.schemas.validations import (
    RUNTIME_VERSION,
    ExecutionResult,
    RuntimeIdentity,
    SandboxLimits,
    Variant,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandResult:
    code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    exceeded: bool


def docker_command(
    args: list[str], *, timeout: float, limit: int, data: bytes | None = None
) -> CommandResult:
    # Docker control credentials are runner-owned. Never pass application environment
    # or secrets to containers; even the CLI sees only its required connection settings.
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    try:
        process = subprocess.Popen(
            ["docker", *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
    except OSError as exc:
        raise SandboxUnavailableError from exc
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    buffers = [bytearray(), bytearray()]
    exceeded = threading.Event()

    def drain(index: int) -> None:
        stream = process.stdout if index == 0 else process.stderr
        assert stream is not None
        while chunk := stream.read(4096):
            remaining = max(0, limit - len(buffers[index]))
            buffers[index].extend(chunk[:remaining])
            if len(chunk) > remaining:
                exceeded.set()
                process.kill()
        stream.close()

    def write() -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(data or b"")
            process.stdin.close()
        except (BrokenPipeError, OSError):
            # The child can exit before consuming stdin; its exit status is authoritative.
            process.stdin.close()

    readers = [threading.Thread(target=drain, args=(i,), daemon=True) for i in range(2)]
    sender = threading.Thread(target=write, daemon=True)
    for thread in [*readers, sender]:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.wait(timeout=2)
    for thread in [*readers, sender]:
        thread.join(timeout=2)
    return CommandResult(
        process.returncode, bytes(buffers[0]), bytes(buffers[1]), timed_out, exceeded.is_set()
    )


class DockerValidationRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _command(
        self,
        args: list[str],
        *,
        timeout: float | None = None,
        limit: int = 32768,
        data: bytes | None = None,
    ) -> CommandResult:
        return docker_command(
            args,
            timeout=timeout or self.settings.validation_command_timeout_seconds,
            limit=limit,
            data=data,
        )

    @staticmethod
    def _check(result: CommandResult) -> None:
        if result.timed_out:
            raise SandboxUnavailableError
        if result.exceeded:
            raise SandboxPolicyError
        if result.code:
            message = result.stderr.lower()
            if any(
                s in message
                for s in (
                    b"cannot connect",
                    b"error during connect",
                    b"daemon is not running",
                    b"connection refused",
                    b"connection reset",
                )
            ):
                raise SandboxUnavailableError
            raise SandboxPolicyError

    def _json(self, args: list[str]) -> object:
        result = self._command(args)
        self._check(result)
        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            raise SandboxPolicyError from exc

    def prepare(self, image_reference: str) -> RuntimeIdentity:
        info = self._json(["info", "--format", "{{json .}}"])
        if (
            not isinstance(info, dict)
            or info.get("OSType") != "linux"
            or not all(info.get(key) for key in ("MemoryLimit", "PidsLimit", "CpuCfsQuota"))
            or not any(
                isinstance(option, str) and option.split(",", 1)[0] == "name=seccomp"
                for option in info.get("SecurityOptions", [])
            )
        ):
            raise SandboxPolicyError
        images = self._json(["image", "inspect", image_reference])
        if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
            raise SandboxPolicyError
        image = images[0]
        config = image.get("Config", {})
        labels = config.get("Labels", {})
        digest = bundle_digest()
        if (
            labels.get("org.traceguard.runtime") != RUNTIME_VERSION
            or labels.get("org.traceguard.bundle") != digest
            or image.get("Os") != "linux"
            or config.get("User") != "10001:10001"
            or config.get("Entrypoint") != ["python", "-I", "/opt/runner/entrypoint.py"]
            or config.get("Volumes")
        ):
            raise SandboxPolicyError
        return RuntimeIdentity(
            image_reference=image_reference,
            image_id=image["Id"],
            repository_digest=next(iter(image.get("RepoDigests") or []), None),
            bundle_digest=digest,
            limits=SandboxLimits.from_settings(self.settings),
        )

    def create_args(
        self,
        runtime: RuntimeIdentity,
        variant: Variant,
        validation_id: UUID,
        token: UUID,
        name: str,
    ) -> list[str]:
        s = self.settings
        return [
            "create",
            "--name",
            name,
            "--interactive",
            "--network",
            "none",
            "--read-only",
            "--user",
            "10001:10001",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            # Docker's daemon default can be unconfined; require its built-in filter.
            "--security-opt",
            "seccomp=builtin",
            "--memory",
            f"{s.validation_memory_mb}m",
            "--memory-swap",
            f"{s.validation_memory_mb}m",
            "--cpus",
            str(s.validation_cpus),
            "--pids-limit",
            str(s.validation_pids),
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={s.validation_tmp_mb}m,mode=1777",
            "--ulimit",
            "core=0:0",
            "--ulimit",
            "nofile=128:128",
            "--log-driver",
            "none",
            "--stop-timeout",
            "1",
            "--ipc",
            "none",
            "--label",
            f"org.traceguard.validation={validation_id}",
            "--label",
            f"org.traceguard.claim={token}",
            "--label",
            f"org.traceguard.expires={int(time.time()) + s.validation_timeout_seconds + 60}",
            "--label",
            f"org.traceguard.managed={RUNTIME_VERSION}",
            "--env",
            f"TRACEGUARD_VARIANT={variant}",
            "--env",
            f"TRACEGUARD_TIMEOUT={s.validation_timeout_seconds}",
            "--env",
            f"TRACEGUARD_OUTPUT_LIMIT={s.validation_output_bytes}",
            "--env",
            f"TRACEGUARD_RESULT_LIMIT={s.validation_result_bytes}",
            "--env",
            f"TRACEGUARD_SOURCE_LIMIT={s.generated_test_max_source_bytes}",
            runtime.image_id,
        ]

    def _remove(self, name: str) -> None:
        result = self._command(["rm", "--force", name])
        if (
            result.code
            and b"No such container" not in result.stderr
            or result.timed_out
            or result.exceeded
        ):
            raise SandboxCleanupError

    def cleanup_expired(self) -> None:
        self._cleanup(None, None)

    def cleanup_previous(self, validation_id: UUID, token: UUID) -> None:
        self._cleanup(validation_id, token)

    def _cleanup(self, validation_id: UUID | None, token: UUID | None) -> None:
        args = [
            "ps",
            "--all",
            "--filter",
            f"label=org.traceguard.managed={RUNTIME_VERSION}",
            "--format",
            "{{.ID}}",
        ]
        if validation_id is not None:
            args += ["--filter", f"label=org.traceguard.validation={validation_id}"]
        result = self._command(args)
        self._check(result)
        for identifier in result.stdout.decode().splitlines():
            items = self._json(["inspect", identifier])
            if not isinstance(items, list) or len(items) != 1:
                raise SandboxPolicyError
            item = items[0]
            labels = item["Config"]["Labels"]
            previous = token is not None and labels.get("org.traceguard.claim") != str(token)
            expired = int(labels.get("org.traceguard.expires", "0")) <= time.time()
            if item["Name"].startswith("/traceguard-validation-") and (previous or expired):
                self._remove(identifier)

    def execute(
        self,
        bundle: ExecutionBundle,
        runtime: RuntimeIdentity,
        variant: Variant,
        validation_id: UUID,
        token: UUID,
    ) -> ExecutionResult:
        if (
            variant not in {"faulty", "corrected"}
            or runtime.limits != SandboxLimits.from_settings(self.settings)
            or len(bundle.source.encode()) > self.settings.generated_test_max_source_bytes
        ):
            raise SandboxPolicyError
        name = f"traceguard-validation-{uuid4().hex}"
        started = time.monotonic()
        try:
            created = self._command(self.create_args(runtime, variant, validation_id, token, name))
            self._check(created)
            identifier = created.stdout.decode().strip()
            attached = self._command(
                ["start", "--attach", "--interactive", identifier],
                timeout=self.settings.validation_timeout_seconds + 15,
                limit=self.settings.validation_result_bytes * 6 + 4096,
                data=bundle.source.encode("utf-8"),
            )
            states = self._json(["inspect", identifier])
            if not isinstance(states, list) or len(states) != 1:
                raise SandboxPolicyError
            state = states[0]["State"]
            payload: dict[str, object] = {}
            try:
                decoded = json.loads(attached.stdout)
                if isinstance(decoded, dict):
                    payload = decoded
            except ValueError:
                payload = {}
            valid = (
                set(payload) == {"exit_code", "timed_out", "output_truncated", "junit"}
                and type(payload.get("exit_code")) is int
                and type(payload.get("timed_out")) is bool
                and type(payload.get("output_truncated")) is bool
                and (payload.get("junit") is None or isinstance(payload.get("junit"), str))
            )
            code = int(state["ExitCode"])
            junit = payload.get("junit") if valid and code == 0 else None
            result = parse_result(
                variant=variant,
                container_id=identifier,
                duration_ms=int((time.monotonic() - started) * 1000),
                exit_code=int(str(payload["exit_code"])) if valid and code == 0 else code,
                junit=junit if isinstance(junit, str) else None,
                test_name=bundle.test_name,
                max_result_bytes=self.settings.validation_result_bytes,
                timed_out=attached.timed_out or bool(payload.get("timed_out"))
                if valid
                else attached.timed_out,
                oom=bool(state["OOMKilled"]),
                output_truncated=attached.exceeded or bool(payload.get("output_truncated"))
                if valid
                else attached.exceeded,
            )
            logger.info(
                "Sandbox completed",
                extra={
                    "stage": "sandbox",
                    "variant": variant,
                    "container_id": identifier,
                    "duration_ms": result.duration_ms,
                    "status": result.outcome,
                },
            )
            return result
        finally:
            # Name is known before create, covering uncertain Docker acknowledgements.
            self._remove(name)
