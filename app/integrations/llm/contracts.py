from typing import Protocol

from app.schemas.attributions import AttributionRequest, LLMResult


class LLMClient(Protocol):
    def attribute(self, request: AttributionRequest) -> LLMResult:
        """Return a structured diagnosis plus safe usage, or a translated application error."""
        ...
