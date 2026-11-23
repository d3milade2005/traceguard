import json

from app.core.exceptions import LLMConfigurationError, LLMTransientError
from app.schemas.attributions import (
    AttributionDecision,
    AttributionRequest,
    EvidenceReference,
    LLMResult,
    ProviderUsage,
)


class FakeLLMClient:
    """Deterministic provider substitute; never opens a connection."""

    def __init__(self, mode: str = "attributed") -> None:
        self.mode = mode
        self.requests: list[AttributionRequest] = []

    def attribute(self, request: AttributionRequest) -> LLMResult:
        self.requests.append(request)
        if self.mode == "transient":
            raise LLMTransientError
        if self.mode == "permanent":
            raise LLMConfigurationError
        if self.mode == "abstain":
            decision = AttributionDecision(
                decision="abstain",
                responsible_agent=None,
                decisive_sequence=None,
                failure_category=None,
                explanation="Visible evidence is insufficient.",
                confidence="low",
                evidence=(),
                abstention_reason="The decisive evidence is unavailable.",
            )
        else:
            steps = json.loads(request.evidence_json)["steps"]
            step = next(step for step in reversed(steps) if step["agent_id"] is not None)
            sequence = step["sequence"] + (10000 if self.mode == "invalid_sequence" else 0)
            decision = AttributionDecision(
                decision="attributed",
                responsible_agent="unseen-agent"
                if self.mode == "unknown_agent"
                else step["agent_id"],
                decisive_sequence=sequence,
                failure_category=None,
                explanation="The visible action violates the requested task.",
                confidence="medium",
                evidence=(
                    EvidenceReference(
                        start_sequence=sequence,
                        end_sequence=sequence,
                        reason="Visible task and action.",
                    ),
                ),
                abstention_reason=None,
            )
        return LLMResult(
            attribution=decision,
            usage=ProviderUsage(
                response_id="resp_fake",
                response_model=request.model,
                input_tokens=100,
                output_tokens=80,
                reasoning_tokens=20,
                latency_ms=1,
            ),
        )
