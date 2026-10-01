import json

import httpx
import pytest
from openai import OpenAI as OfficialOpenAI
from pydantic import SecretStr

from app.core.exceptions import (
    LLMConfigurationError,
    LLMIncompleteError,
    LLMOutputError,
    LLMOutputLimitError,
    LLMRefusalError,
    LLMTransientError,
)
from app.integrations.llm import openai as integration
from app.schemas.attributions import AttributionDecision, AttributionRequest
from tests.unit.test_attribution_contracts import decision


def request():
    return AttributionRequest(
        prompt_version="attribution-v1",
        instructions="Trusted instructions",
        evidence_json='{"task":"untrusted evidence"}',
        model="gpt-5.6-luna",
        reasoning_effort="medium",
    )


def response_body():
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": "gpt-5.6-luna",
        "output": [
            {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": decision().model_dump_json(), "annotations": []}
                ],
            }
        ],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 30,
            "total_tokens": 40,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 10},
        },
    }


def adapter(monkeypatch, handler):
    calls = []
    options = []

    def transport(request):
        calls.append(request)
        return handler(request)

    def sdk(**kwargs):
        options.append(kwargs)
        return OfficialOpenAI(
            **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(transport))
        )

    monkeypatch.setattr(integration, "OpenAI", sdk)
    return (
        integration.OpenAILLMClient(
            SecretStr("offline_fake_key"), timeout_seconds=30, max_output_tokens=2048
        ),
        calls,
        options,
    )


def test_real_sdk_requests_strict_responses_without_tools_storage_or_sdk_retries(monkeypatch):
    client, calls, options = adapter(
        monkeypatch, lambda _: httpx.Response(200, json=response_body())
    )
    result = client.attribute(request())
    assert result.attribution == decision()
    assert len(calls) == 1
    body = json.loads(calls[0].content)
    assert calls[0].url.path == "/v1/responses"
    assert body["model"] == "gpt-5.6-luna"
    assert body["reasoning"] == {"effort": "medium"}
    assert body["store"] is False
    assert body["tools"] == [] and body["tool_choice"] == "none"
    assert body["truncation"] == "disabled"
    assert body["max_output_tokens"] == 2048
    assert body["instructions"] == "Trusted instructions"
    assert body["input"] == [{"role": "user", "content": request().evidence_json}]
    schema = body["text"]["format"]
    assert schema["type"] == "json_schema" and schema["strict"] is True
    assert schema["schema"]["additionalProperties"] is False
    assert set(schema["schema"]["required"]) == set(AttributionDecision.model_fields)
    assert options[0]["max_retries"] == 0
    assert options[0]["timeout"] == 30
    assert calls[0].extensions["timeout"]["read"] == 30
    assert result.usage.response_id == "resp_test"
    assert result.usage.input_tokens == 10
    assert result.usage.reasoning_tokens == 10


@pytest.mark.parametrize(
    "status,expected",
    [
        (400, LLMConfigurationError),
        (401, LLMConfigurationError),
        (403, LLMConfigurationError),
        (429, LLMTransientError),
        (500, LLMTransientError),
        (503, LLMTransientError),
    ],
)
def test_provider_http_errors_are_translated_and_never_sdk_retried(monkeypatch, status, expected):
    client, calls, _ = adapter(
        monkeypatch,
        lambda _: httpx.Response(
            status,
            json={
                "error": {"message": "secret provider detail", "type": "error", "code": "temporary"}
            },
        ),
    )
    with pytest.raises(expected):
        client.attribute(request())
    assert len(calls) == 1


def test_insufficient_quota_is_permanent(monkeypatch):
    client, _, _ = adapter(
        monkeypatch,
        lambda _: httpx.Response(
            429, json={"error": {"message": "quota", "type": "error", "code": "insufficient_quota"}}
        ),
    )
    with pytest.raises(LLMConfigurationError):
        client.attribute(request())


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError])
def test_network_failures_are_translated(monkeypatch, error):
    def unavailable(request):
        raise error("offline", request=request)

    client, calls, _ = adapter(monkeypatch, unavailable)
    with pytest.raises(LLMTransientError):
        client.attribute(request())
    assert len(calls) == 1


@pytest.mark.parametrize(
    "case,expected",
    [
        ("refusal", LLMRefusalError),
        ("incomplete", LLMIncompleteError),
        ("limit", LLMOutputLimitError),
        ("partial_limit", LLMOutputLimitError),
        ("missing", LLMOutputError),
        ("invalid_schema", LLMOutputError),
    ],
)
def test_response_edge_cases_are_not_silently_repaired(monkeypatch, case, expected):
    body = response_body()
    if case == "refusal":
        body["output"][0]["content"] = [{"type": "refusal", "refusal": "Cannot comply"}]
    elif case in {"incomplete", "limit", "partial_limit"}:
        body["status"] = "incomplete"
        body["incomplete_details"] = {
            "reason": "max_output_tokens" if case != "incomplete" else "content_filter"
        }
        if case == "partial_limit":
            body["output"][0]["content"][0]["text"] = '{"decision":"attr'
        else:
            body["output"] = []
    elif case == "missing":
        body["output"] = []
    else:
        body["output"][0]["content"][0]["text"] = '{"decision":"attributed"}'
    client, _, _ = adapter(monkeypatch, lambda _: httpx.Response(200, json=body))
    with pytest.raises(expected) as failure:
        client.attribute(request())
    assert failure.value.usage.input_tokens == 10


def test_absent_key_fails_only_when_real_inference_is_requested():
    client = integration.OpenAILLMClient(None, timeout_seconds=30, max_output_tokens=2048)
    with pytest.raises(LLMConfigurationError):
        client.attribute(request())


@pytest.mark.parametrize("code", ["server_error", "rate_limit_exceeded"])
def test_failed_response_with_transient_error_is_retryable(monkeypatch, code):
    body = response_body()
    body.update(status="failed", error={"code": code, "message": "private provider detail"})
    body["output"] = []
    client, calls, _ = adapter(monkeypatch, lambda _: httpx.Response(200, json=body))
    with pytest.raises(LLMTransientError):
        client.attribute(request())
    assert len(calls) == 1
