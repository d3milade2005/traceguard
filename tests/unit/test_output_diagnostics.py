import hashlib
import json

import httpx
import pytest

from app.core.exceptions import LLMOutputError
from evaluation.artifacts import safe_failure
from tests.unit.test_openai_attribution import adapter, request, response_body


@pytest.mark.parametrize(
    "payload",
    [
        {"decision": "attributed", "credential_secret_in_unknown_key": "rejected-secret-value"},
        {"decision": "invalid-rejected-secret-value"},
        None,
    ],
)
def test_safe_schema_diagnostics_never_include_values_messages_or_unknown_field_names(
    monkeypatch, payload
):
    body = response_body()
    text = json.dumps(payload) if payload is not None else "{rejected-secret-value"
    body["output"][0]["content"][0]["text"] = text
    client, calls, _ = adapter(monkeypatch, lambda _: httpx.Response(200, json=body))
    with pytest.raises(LLMOutputError) as caught:
        client.attribute(request())
    error = caught.value
    serialized = json.dumps(safe_failure(error))
    assert "rejected-secret-value" not in serialized
    assert "credential_secret_in_unknown_key" not in serialized
    assert "ctx" not in serialized and "input_value" not in serialized
    assert error.diagnostics.schema_name == "AttributionDecision"
    assert error.diagnostics.prompt_version == "attribution-v1"
    assert error.diagnostics.response_sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert error.diagnostics.error_count > 0
    assert error.usage.response_id == "resp_test" and error.usage.latency_ms is not None
    assert len(calls) == 1
