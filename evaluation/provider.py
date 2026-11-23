"""Offline recovery of journaled visible responses; never makes provider requests."""

from openai.types.responses import Response
from pydantic import BaseModel

from app.integrations.llm.openai import parse_structured_response
from app.schemas.attributions import AttributionDecision, AttributionRequest, LLMResult

CAPTURE_VERSION = "visible-provider-response-v1"
API_BASE_URL = "https://api.openai.com/v1"


def recover_response[T: BaseModel](event: dict, request: BaseModel, response_type: type[T]) -> T:
    if not isinstance(request, AttributionRequest) or response_type is not LLMResult:
        raise ValueError("Only attribution response recovery is supported")
    # SDK model_dump preserves Python field names such as schema_, whereas the
    # wire format uses aliases such as schema. Accept both when reloading evidence.
    response = Response.model_validate(event["response"], by_name=True)
    decision, usage = parse_structured_response(
        response, AttributionDecision, request.prompt_version, event["latency_ms"]
    )
    return response_type.model_validate_json(
        LLMResult(attribution=decision, usage=usage).model_dump_json()
    )
