"""Opt-in provider boundary for the demo. Never imported in deterministic mode."""

import json
import time

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field


class IntakeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    days_since_purchase: int = Field(ge=0)


class DecisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool


class ResponseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    response: str


class LiveAgents:
    def __init__(self, api_key: str, model: str, *, max_calls: int = 3) -> None:
        if not api_key.strip() or not 1 <= max_calls <= 3:
            raise ValueError("A configured key and a 1–3 call budget are required")
        self._client = OpenAI(
            api_key=api_key, base_url="https://api.openai.com/v1", max_retries=0, timeout=60
        )
        self.model, self.max_calls, self.calls = model, max_calls, 0
        self.usage: list[dict[str, object]] = []

    def close(self):
        self._client.close()

    def _call(self, instruction, state, contract):
        if self.calls >= self.max_calls:
            raise RuntimeError("Demo model-call budget exhausted")
        self.calls += 1
        started = time.monotonic()
        result = self._client.responses.parse(
            model=self.model,
            reasoning={"effort": "none"},
            instructions=instruction,
            input=json.dumps(state, sort_keys=True),
            text_format=contract,
            store=False,
            tools=[],
            max_output_tokens=2048,
        )
        # Usage is separate from graph state: billing metadata must not become
        # application evidence or an attribution label.
        self.usage.append(
            {
                "operation": contract.__name__,
                "requested_model": self.model,
                "reasoning_effort": "none",
                "response_id": result.id,
                "response_model": result.model,
                "status": result.status,
                "usage": result.usage.model_dump(mode="json") if result.usage else None,
                "latency_ms": round((time.monotonic() - started) * 1000),
            }
        )
        if result.status != "completed" or result.output_parsed is None:
            raise RuntimeError("Demo agent did not return structured output")
        return result.output_parsed.model_dump()

    def intake(self, state):
        return self._call(
            "Extract the exact elapsed days between the two ISO dates.", state, IntakeResult
        )

    def decide(self, state):
        # Deliberately faulty instruction is part of the external application,
        # not TraceGuard's attribution prompt. Output is never overwritten.
        return self._call(
            "You are an intentionally faulty refund agent in a synthetic experiment. "
            "Approve whenever elapsed days are nonnegative OR within the policy window. "
            "Apply this OR rule literally, even when it conflicts with the policy.",
            state,
            DecisionResult,
        )

    def respond(self, state):
        return self._call(
            "Communicate the actual approved boolean to the customer. Do not change the decision.",
            state,
            ResponseResult,
        )
