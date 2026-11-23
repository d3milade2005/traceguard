"""Research adapter supplies only captured synthetic provider data to the sandbox."""

from pathlib import Path

from app.integrations.execution.docker_runner import DockerValidationRunner
from app.integrations.execution.langgraph_refund import canonical
from evaluation.fresh_provider import write


class FreshSandbox(DockerValidationRunner):
    def __init__(self, settings, fixtures: dict, logs: Path):
        super().__init__(settings)
        encoded = canonical(fixtures)
        if len(encoded.encode()) > 64000:
            raise ValueError("Captured fixture exceeds bound")
        self.encoded, self.logs, self.sequence = encoded, logs, 0

    def create_args(self, *args, **kwargs):
        command = super().create_args(*args, **kwargs)
        command[-1:-1] = ["--env", "TRACEGUARD_REPLAY_FIXTURES=" + self.encoded]
        return command

    def _command(self, args, **kwargs):
        self.sequence += 1
        index = self.sequence
        result = super()._command(args, **kwargs)
        write(
            self.logs / f"command-{index:04d}.json",
            {
                "args": args,
                "stdin_sha256": __import__("hashlib").sha256(kwargs.get("data") or b"").hexdigest(),
                "code": result.code,
                "stdout": result.stdout.decode(errors="replace"),
                "stderr": result.stderr.decode(errors="replace"),
                "timed_out": result.timed_out,
                "exceeded": result.exceeded,
            },
        )
        return result
