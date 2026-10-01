import time

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError
from pydantic import SecretStr, ValidationError

from app.core.exceptions import (
    LLMConfigurationError,
    LLMIncompleteError,
    LLMOutputError,
    LLMOutputLimitError,
    LLMProviderError,
    LLMRefusalError,
    LLMTransientError,
)
from app.schemas.attributions import (
    AttributionDecision,
    AttributionRequest,
    LLMResult,
    ProviderUsage,
)


class OpenAILLMClient:
    """Responses-only inference; application retries own the single-request SDK boundary."""

    def __init__(
        self, api_key: SecretStr | None, *, timeout_seconds: int, max_output_tokens: int
    ) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens

    def attribute(self, request: AttributionRequest) -> LLMResult:
        if self.api_key is None or not self.api_key.get_secret_value().strip():
            raise LLMConfigurationError
        started = time.monotonic()
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
                            "name": "AttributionDecision",
                            "strict": True,
                            "schema": AttributionDecision.model_json_schema(),
                        }
                    },
                    store=False,
                    tools=[],
                    tool_choice="none",
                    max_output_tokens=self.max_output_tokens,
                    timeout=self.timeout_seconds,
                    truncation="disabled",
                )
        except (APITimeoutError, APIConnectionError) as exc:
            raise LLMTransientError from exc
        except RateLimitError as exc:
            if exc.code == "insufficient_quota":
                raise LLMConfigurationError from exc
            raise LLMTransientError from exc
        except APIStatusError as exc:
            if exc.status_code >= 500 or exc.status_code in {408, 409}:
                raise LLMTransientError from exc
            raise LLMConfigurationError from exc
        except (ValidationError, ValueError) as exc:
            raise LLMOutputError from exc
        usage = ProviderUsage(
            response_id=response.id,
            response_model=response.model,
            input_tokens=response.usage.input_tokens if response.usage else None,
            output_tokens=response.usage.output_tokens if response.usage else None,
            reasoning_tokens=response.usage.output_tokens_details.reasoning_tokens
            if response.usage
            else None,
            latency_ms=round((time.monotonic() - started) * 1000),
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
            raise LLMOutputError(usage=usage)
        try:
            attribution = AttributionDecision.model_validate_json(response.output_text)
        except ValidationError as exc:
            raise LLMOutputError(usage=usage) from exc
        return LLMResult(attribution=attribution, usage=usage)
