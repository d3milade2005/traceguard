"""Use the existing sandbox transport/policy with an explicitly identified suite overlay.

The production image is validated unchanged. Only the final image argument is
replaced by the inspected evaluation image. Both image identities are recorded;
the overlay is never claimed to have the production calculator bundle identity.
"""

import hashlib
from uuid import UUID

from app.core.config import Settings
from app.core.exceptions import SandboxPolicyError
from app.integrations.execution.contracts import ExecutionBundle
from app.integrations.execution.docker_runner import DockerValidationRunner
from app.integrations.execution.registry import bundle_digest
from app.schemas.validations import ExecutionResult, RuntimeIdentity, Variant
from evaluation.artifacts import digest
from evaluation.config import ROOT


def suite_bundle_digest() -> str:
    paths = (
        "evaluation/__init__.py",
        "evaluation/harness.py",
        "evaluation/sandbox/conftest.py",
        "evaluation/sandbox/Dockerfile",
    )
    return digest(
        {
            "production_bundle": bundle_digest(),
            "overlay_files": {
                path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths
            },
        }
    )


class SuiteDockerRunner(DockerValidationRunner):
    def __init__(self, settings: Settings, *, base_image: str, suite_image: str) -> None:
        super().__init__(settings)
        self.base_runtime = super().prepare(base_image)
        images = self._json(["image", "inspect", suite_image])
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
            or labels.get("org.traceguard.evaluation-base") != self.base_runtime.image_id
            or labels.get("org.traceguard.evaluation-bundle") != suite_bundle_digest()
        ):
            raise SandboxPolicyError
        self.suite_image_id = image["Id"]

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
        args[-1] = self.suite_image_id
        return args

    def validate(
        self, bundle: ExecutionBundle, validation_id: UUID
    ) -> tuple[ExecutionResult, ExecutionResult]:
        from uuid import uuid4

        token = uuid4()
        # Existing execute always removes its container in a finally block.
        faulty = self.execute(bundle, self.base_runtime, "faulty", validation_id, token)
        corrected = self.execute(bundle, self.base_runtime, "corrected", validation_id, token)
        return faulty, corrected

    def identity(self) -> dict:
        return {
            "base_runtime": self.base_runtime.model_dump(mode="json"),
            "evaluation_image_id": self.suite_image_id,
            "evaluation_bundle_digest": suite_bundle_digest(),
            "harness_implementation": "controlled-suite-v1",
        }
