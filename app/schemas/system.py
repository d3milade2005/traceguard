from typing import Literal

from pydantic import BaseModel

from app.schemas.attributions import ReasoningEffort


class SystemInfo(BaseModel):
    api_health: Literal["ok"] = "ok"
    environment: Literal["development", "test", "production"]
    model: str
    reasoning_effort: ReasoningEffort
    attribution_prompt_version: str
    context_policy_version: str
    regression_spec_version: str
    regression_prompt_version: str
    compiler_version: str
    validator_version: str
