"""Research-only replay of one fixed developer-owned LangGraph refund example.

This is not registered with production's external-trace replay preflight. A saved
controlled-refund artifact is reused unchanged; its nested input is mapped only
to the identical fixed dates/window of the example. No arbitrary graph, policy,
customer input or model is executable through this adapter.
"""

import hashlib
from uuid import UUID, uuid4

from app.core.config import Settings
from app.core.exceptions import SandboxPolicyError
from app.integrations.execution.contracts import ExecutionBundle
from app.integrations.execution.docker_runner import DockerValidationRunner
from app.integrations.execution.registry import bundle_digest
from app.schemas.validations import ExecutionResult, RuntimeIdentity, Variant
from evaluation.artifacts import digest
from evaluation.config import ROOT

OVERLAY_FILES = (
    "evaluation/__init__.py",
    "evaluation/langgraph_replay.py",
    "evaluation/langgraph_sandbox/harness.py",
    "evaluation/langgraph_sandbox/conftest.py",
    "evaluation/langgraph_sandbox/Dockerfile",
    "evaluation/langgraph_sandbox/requirements.lock",
    "examples/langgraph_refund/application.py",
    "examples/langgraph_refund/integration.py",
    "app/integrations/execution/sandbox/conftest.py",
)


def replay_bundle_digest() -> str:
    return digest(
        {
            "production_bundle": bundle_digest(),
            "overlay_files": {
                path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                for path in OVERLAY_FILES
            },
        }
    )


class LangGraphDockerRunner(DockerValidationRunner):
    """Use production containment while identifying the research overlay separately."""

    def __init__(self, settings: Settings, *, base_image: str, replay_image: str) -> None:
        super().__init__(settings)
        self.base_runtime = super().prepare(base_image)
        images = self._json(["image", "inspect", replay_image])
        if not isinstance(images, list) or len(images) != 1:
            raise SandboxPolicyError
        image = images[0]
        config = image.get("Config", {})
        labels = config.get("Labels", {})
        if (
            image.get("Os") != "linux"
            or config.get("User") != "10001:10001"
            or config.get("Entrypoint") != ["python", "-I", "/opt/runner/entrypoint.py"]
            or config.get("Volumes")
            or labels.get("org.traceguard.langgraph-base") != self.base_runtime.image_id
            or labels.get("org.traceguard.langgraph-bundle") != replay_bundle_digest()
        ):
            raise SandboxPolicyError
        self.replay_image_id = image["Id"]

    def create_args(
        self,
        runtime: RuntimeIdentity,
        variant: Variant,
        validation_id: UUID,
        token: UUID,
        name: str,
    ) -> list[str]:
        if runtime != self.base_runtime:
            raise SandboxPolicyError
        args = super().create_args(runtime, variant, validation_id, token, name)
        args[-1] = self.replay_image_id
        return args

    def validate(
        self, bundle: ExecutionBundle, validation_id: UUID
    ) -> tuple[ExecutionResult, ExecutionResult]:
        token = uuid4()
        faulty = self.execute(bundle, self.base_runtime, "faulty", validation_id, token)
        corrected = self.execute(bundle, self.base_runtime, "corrected", validation_id, token)
        return faulty, corrected

    def identity(self) -> dict:
        return {
            "base_runtime": self.base_runtime.model_dump(mode="json"),
            "replay_image_id": self.replay_image_id,
            "replay_bundle_digest": replay_bundle_digest(),
            "harness_implementation": "fixed-langgraph-refund-replay-v1",
            "inference_calls": 0,
        }
