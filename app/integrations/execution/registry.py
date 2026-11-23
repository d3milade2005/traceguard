import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.core.exceptions import ValidationArtifactError
from app.integrations.execution.calculator import operands
from app.integrations.execution.langgraph_refund import CONTRACT, validate_input
from app.integrations.execution.refund import DECISION_AGENT, validate_refund_input
from app.regression_runtime import HARNESS_ID, HARNESS_VERSION
from app.schemas.regression_specs import RegressionSpec, ScenarioSnapshot


@dataclass(frozen=True)
class HarnessDefinition:
    identifier: str = HARNESS_ID
    version: str = HARNESS_VERSION
    implementation: str = "controlled-agent-invocation-v3"

    def check(self, spec: RegressionSpec, snapshot: ScenarioSnapshot) -> None:
        try:
            if spec.scenario is None or spec.scenario.stimulus.field != "input":
                raise ValueError("Only complete input scenarios are approved")
            if (
                isinstance(snapshot.stimulus, dict)
                and snapshot.stimulus.get("replay_contract") == CONTRACT
            ):
                validate_input(spec.target_agent, snapshot.stimulus, snapshot.preconditions)
            elif spec.target_agent == DECISION_AGENT:
                validate_refund_input(spec.target_agent, snapshot.stimulus, snapshot.preconditions)
            else:
                operands(spec.target_agent or "", snapshot.stimulus, snapshot.preconditions)
        except ValueError as exc:
            raise ValidationArtifactError from exc


def approved_harness(identifier: str, version: str) -> HarnessDefinition:
    if (identifier, version) != (HARNESS_ID, HARNESS_VERSION):
        raise ValidationArtifactError
    return HarnessDefinition()


def bundle_digest() -> str:
    """Pin exactly the trusted code/lock files copied into the sandbox image."""
    app_root = Path(__file__).resolve().parents[2]
    files = [
        app_root / "regression_runtime.py",
        app_root / "integrations/execution/calculator.py",
        app_root / "integrations/execution/refund.py",
        app_root / "integrations/execution/langgraph_refund.py",
        app_root / "integrations/execution/sandbox/conftest.py",
        app_root / "integrations/execution/sandbox/entrypoint.py",
        app_root / "integrations/execution/sandbox/requirements.lock",
    ]
    digest = hashlib.sha256()
    for file in files:
        digest.update(file.name.encode() + b"\0" + file.read_bytes() + b"\0")
    return digest.hexdigest()
