"""Explicit research configuration, independent of application DB/broker settings."""

import hashlib
import platform
from importlib.metadata import version
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.context import CONTEXT_POLICY_VERSION, ContextPolicy
from app.schemas.attributions import ReasoningEffort
from app.services.attribution_input import INSTRUCTIONS, PROMPT_VERSION
from app.services.pytest_compiler import COMPILER_VERSION
from app.services.regression_input import INSTRUCTIONS as REGRESSION_INSTRUCTIONS
from app.services.regression_input import PROMPT_VERSION as REGRESSION_PROMPT_VERSION
from evaluation.artifacts import digest
from evaluation.context import RUN_ID_POLICY
from evaluation.freeze import RUNNER_VERSION
from evaluation.harness import REPLAY_CONTRACT_VERSION
from evaluation.provider import API_BASE_URL, CAPTURE_VERSION

ROOT = Path(__file__).resolve().parents[1]


class ProviderSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    openai_api_key: SecretStr | None = None
    openai_timeout_seconds: int = Field(default=60, ge=1, le=600)
    openai_max_output_tokens: int = Field(default=4096, ge=256, le=32768)


def inference_config(
    model: str,
    effort: ReasoningEffort,
    policy: ContextPolicy,
    provider: ProviderSettings,
    *,
    track: Literal["attribution", "regressions"] = "attribution",
) -> dict:
    if track == "attribution":
        from evaluation.who_when import SCORING_VERSION, SEMANTICS_VERSION

        semantics, scoring = SEMANTICS_VERSION, SCORING_VERSION
    else:
        semantics, scoring = "controlled-suite-v1", "controlled-oracle-exact-v1"
    paths = [
        "evaluation",
        "scripts/evaluate_attribution.py",
        "scripts/evaluate_regressions.py",
        "scripts/build_evaluation_sandbox.py",
        "scripts/compare_evaluations.py",
        "scripts/freeze_evaluation.py",
        "scripts/prepare_attribution.py",
        "scripts/analyze_attribution.py",
        "scripts/verify_attribution.py",
        "pyproject.toml",
        "uv.lock",
        "app/services/context.py",
        "app/core/context.py",
        "app/services/attribution_input.py",
        "app/services/attribution_validation.py",
        "app/integrations/llm",
        "app/core/exceptions.py",
        "app/integrations/traces/who_when.py",
        "app/services/regression_input.py",
        "app/services/regression_validation.py",
        "app/services/pytest_compiler.py",
        "app/regression_runtime.py",
        "app/integrations/execution",
        "app/schemas",
    ]
    files: list[Path] = []
    for relative in paths:
        path = ROOT / relative
        files.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    config = {
        "version": "phase8-v3",
        "runner_version": RUNNER_VERSION,
        "model": model,
        "reasoning_effort": effort,
        "prompt_version": PROMPT_VERSION,
        "prompt_digest": digest(INSTRUCTIONS),
        "regression_prompt_version": REGRESSION_PROMPT_VERSION,
        "regression_prompt_digest": digest(REGRESSION_INSTRUCTIONS),
        "context_policy_version": CONTEXT_POLICY_VERSION,
        "context_policy": policy.model_dump(mode="json"),
        "run_id_policy": RUN_ID_POLICY,
        "semantics_version": semantics,
        "scoring_version": scoring,
        "compiler_version": COMPILER_VERSION,
        "replay_contract_version": REPLAY_CONTRACT_VERSION if track == "regressions" else None,
        "timeout_seconds": provider.openai_timeout_seconds,
        "max_output_tokens": provider.openai_max_output_tokens,
        "source_digest": digest(
            {
                str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(
                    p.read_text(encoding="utf-8").encode("utf-8")
                ).hexdigest()
                for p in files
            }
        ),
    }
    if track == "attribution":
        from app.schemas.attributions import AttributionDecision

        config |= {
            "provider_capture_version": CAPTURE_VERSION,
            "api_base_url": API_BASE_URL,
            "service_tier": "default",
            "sdk_max_retries": 0,
            "sample_retries": 0,
            "response_schema_digest": digest(AttributionDecision.model_json_schema()),
            "runtime": {
                "python": platform.python_version(),
                "packages": {
                    name: version(name)
                    for name in ("openai", "pydantic", "pydantic-settings", "pyarrow", "httpx2")
                },
            },
        }
    return config
