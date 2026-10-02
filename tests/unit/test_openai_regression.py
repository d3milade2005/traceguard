import json

import httpx
import pytest

from app.core.exceptions import LLMOutputError, LLMOutputLimitError, LLMRefusalError
from app.schemas.regression_specs import RegressionRequest, RegressionSpec
from tests.unit.test_openai_attribution import adapter, response_body
from tests.unit.test_regression_specs import spec


def request():
    return RegressionRequest(
        prompt_version="regression-spec-v1",
        instructions="Trusted instructions",
        evidence_json='{"task":"Return 4"}',
        model="gpt-5.6-luna",
        reasoning_effort="medium",
    )


def body():
    result = response_body()
    result["output"][0]["content"][0]["text"] = spec().model_dump_json()
    return result


def test_real_sdk_sends_strict_compatible_schema_without_tools_or_retries(monkeypatch):
    client, calls, options = adapter(monkeypatch, lambda _: httpx.Response(200, json=body()))
    result = client.generate(request())
    payload = json.loads(calls[0].content)
    assert payload["store"] is False and payload["tools"] == []
    assert payload["tool_choice"] == "none" and payload["truncation"] == "disabled"
    assert payload["reasoning"] == {"effort": "medium"}
    schema = payload["text"]["format"]
    assert schema["name"] == "RegressionSpec" and schema["strict"] is True
    assert schema["schema"]["additionalProperties"] is False
    assert set(schema["schema"]["required"]) == set(RegressionSpec.model_fields)
    # All nested objects also comply with strict Structured Outputs.
    for definition in schema["schema"]["$defs"].values():
        if definition.get("type") == "object":
            assert definition["additionalProperties"] is False
            assert set(definition["required"]) == set(definition["properties"])
    assert options[0]["max_retries"] == 0 and len(calls) == 1
    assert result.spec == spec() and result.usage.input_tokens == 10


@pytest.mark.parametrize(
    "mode,error",
    [
        ("refusal", LLMRefusalError),
        ("limit", LLMOutputLimitError),
        ("invalid", LLMOutputError),
        ("code", LLMOutputError),
    ],
)
def test_invalid_refused_and_incomplete_output_preserves_usage(monkeypatch, mode, error):
    response = body()
    if mode == "refusal":
        response["output"][0]["content"] = [{"type": "refusal", "refusal": "No"}]
    elif mode == "limit":
        response["status"] = "incomplete"
        response["incomplete_details"] = {"reason": "max_output_tokens"}
        response["output"][0]["content"][0]["text"] = '{"version":'
    else:
        response["output"][0]["content"][0]["text"] = (
            spec().model_copy(update={"title": "exec(payload)"}).model_dump_json()
            if mode == "code"
            else "{}"
        )
    client, calls, _ = adapter(monkeypatch, lambda _: httpx.Response(200, json=response))
    with pytest.raises(error) as failure:
        client.generate(request())
    assert failure.value.usage.input_tokens == 10 and len(calls) == 1
