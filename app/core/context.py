from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTEXT_POLICY_VERSION = "phase3-structural-v1"


class ContextPolicy(BaseModel):
    """Model-independent limits for the temporary analysis representation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    budget: int = Field(default=65536, ge=1)
    reserved_headroom: int = Field(default=4096, ge=0)
    head_steps: int = Field(default=2, ge=1)
    tail_steps: int = Field(default=4, ge=1)
    neighbour_steps: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_headroom(self) -> Self:
        if self.reserved_headroom >= self.budget:
            raise ValueError("Context headroom must be smaller than the budget")
        return self

    @property
    def effective_budget(self) -> int:
        return self.budget - self.reserved_headroom
