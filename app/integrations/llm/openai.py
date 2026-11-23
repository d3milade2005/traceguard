import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, TypeVar

from openai import APIConnectionError, APIStatusError, OpenAI, RateLimitError, omit
from openai.types.responses import Response
from pydantic import BaseModel, SecretStr, ValidationError

from app.core.exceptions import (
    LLMConfigurationError,
    LLMIncompleteError,
    LLMOutputError,
    LLMOutputLimitError,
    LLMProviderError,
    LLMRefusalError,
    LLMTransientError,
)
from app.integrations.llm.diagnostics import output_diagnostics
from app.schemas.attributions import (
    AttributionDecision,
    AttributionRequest,
    LLMResult,
    ProviderUsage,
)
from app.schemas.regression_specs import RegressionRequest, RegressionResult, RegressionSpec

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger(__name__)


class OpenAILLMClient:
    """Responses-only inference; application retries own the single-request SDK boundary."""

    def __init__(
        self,
        api_key: SecretStr | None,
        *,
        timeout_seconds: int,
        max_output_tokens: int,
        service_tier: Literal["default"] | None = None,
        observer: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.service_tier = service_tier
        self.observer = observer

    def attribute(self, request: AttributionRequest) -> LLMResult:
        decision, usage = self._generate(request, AttributionDecision)
        return LLMResult(attribution=decision, usage=usage)

    def generate(self, request: RegressionRequest) -> RegressionResult:
        spec, usage = self._generate(request, RegressionSpec)
        return RegressionResult(spec=spec, usage=usage)

    def _generate(self, request: AttributionRequest, contract: type[T]) -> tuple[T, ProviderUsage]:
        if self.api_key is None or not self.api_key.get_secret_value().strip():
            raise LLMConfigurationError
        started = time.monotonic()
        started_at = datetime.now(UTC).isoformat()
        try:
            with OpenAI(
                api_key=self.api_key.get_secret_value(),
                base_url="https://api.openai.com/v1",
                timeout=self.timeout_seconds,
                max_retries=0,
            ) as client:
                # Inspect refusal/incomplete status before parsing output text. The SDK's
                # parse helper otherwise tries to validate partially generated JSON first.
                response = client.responses.create(
                    model=request.model,
                    reasoning={"effort": request.reasoning_effort},
                    instructions=request.instructions,
                    input=[{"role": "user", "content": request.evidence_json}],
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": contract.__name__,
                            "strict": True,
                            "schema": contract.model_json_schema(),
                        }
                    },
                    store=False,
                    tools=[],
                    tool_choice="none",
                    max_output_tokens=self.max_output_tokens,
                    timeout=self.timeout_seconds,
                    truncation="disabled",
                    service_tier=self.service_tier or omit,
                )
        except (APIConnectionError, APIStatusError) as exc:
            if self.observer is not None:
                # Provider error messages can echo credentials. Preserve safe transport
                # facts, never headers, request objects, or the exception's message/body.
                self.observer(
                    {
                        "kind": "transport_error",
                        "started_at": started_at,
                        "finished_at": datetime.now(UTC).isoformat(),
                        "latency_ms": round((time.monotonic() - started) * 1000),
                        "error_type": type(exc).__name__,
                        "status_code": exc.status_code if isinstance(exc, APIStatusError) else None,
                        "request_id": exc.request_id if isinstance(exc, APIStatusError) else None,
                    }
                )
            if isinstance(exc, APIConnectionError):
                raise LLMTransientError from exc
            if isinstance(exc, RateLimitError):
                if exc.code == "insufficient_quota":
                    raise LLMConfigurationError from exc
                raise LLMTransientError from exc
            if exc.status_code >= 500 or exc.status_code in {408, 409}:
                raise LLMTransientError from exc
            raise LLMConfigurationError from exc
        except (ValidationError, ValueError) as exc:
            raise LLMOutputError from exc
        latency_ms = round((time.monotonic() - started) * 1000)
        if self.observer is not None:
            body = response.model_dump(mode="json")
            # Only visible output is research evidence; private reasoning is not.
            visible = [item for item in body["output"] if item.get("type") != "reasoning"]
            omitted = len(body["output"]) - len(visible)
            body["output"] = visible
            self.observer(
                {
                    "kind": "response",
                    "started_at": started_at,
                    "finished_at": datetime.now(UTC).isoformat(),
                    "latency_ms": latency_ms,
                    "request_id": getattr(response, "_request_id", None),
                    "omitted_reasoning_items": omitted,
                    "response": body,
                }
            )
        return parse_structured_response(response, contract, request.prompt_version, latency_ms)


def parse_structured_response[Parsed: BaseModel](
    response: Response, contract: type[Parsed], prompt_version: str, latency_ms: int
) -> tuple[Parsed, ProviderUsage]:
    """Validate a received response; also permits recovery from a saved research response."""
    usage = ProviderUsage(
        response_id=response.id,
        response_model=response.model,
        input_tokens=response.usage.input_tokens if response.usage else None,
        output_tokens=response.usage.output_tokens if response.usage else None,
        reasoning_tokens=response.usage.output_tokens_details.reasoning_tokens
        if response.usage
        else None,
        latency_ms=latency_ms,
    )
    if response.status == "incomplete":
        if (
            response.incomplete_details
            and response.incomplete_details.reason == "max_output_tokens"
        ):
            raise LLMOutputLimitError(usage=usage)
        raise LLMIncompleteError(usage=usage)
    if response.status != "completed":
        if response.error and response.error.code in {"server_error", "rate_limit_exceeded"}:
            raise LLMTransientError
        raise LLMProviderError
    if any(
        part.type == "refusal"
        for item in response.output
        if item.type == "message"
        for part in item.content
    ):
        raise LLMRefusalError(usage=usage)
    if not response.output_text:
        diagnostics = output_diagnostics(contract, prompt_version, "", None)
        logger.warning(
            "Structured output missing",
            extra={
                "output_diagnostics": diagnostics.model_dump(mode="json"),
                **usage.model_dump(exclude_none=True),
            },
        )
        raise LLMOutputError(usage=usage, diagnostics=diagnostics)
    try:
        parsed = contract.model_validate_json(response.output_text)
    except ValidationError as exc:
        diagnostics = output_diagnostics(contract, prompt_version, response.output_text, exc)
        logger.warning(
            "Structured output rejected",
            extra={
                "output_diagnostics": diagnostics.model_dump(mode="json"),
                **usage.model_dump(exclude_none=True),
            },
        )
        raise LLMOutputError(usage=usage, diagnostics=diagnostics) from exc
    return parsed, usage
