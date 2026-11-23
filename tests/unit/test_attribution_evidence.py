"""All provider bodies in this module are offline fixtures, never research predictions."""

import json
from unittest.mock import Mock

import httpx
import pytest

from app.core.exceptions import LLMOutputError
from app.schemas.attributions import LLMResult
from evaluation.artifacts import Experiment, RecordedCallFailure, read_json
from evaluation.preparation import cost_bound, request_body
from tests.unit.test_openai_attribution import adapter, request, response_body


def terra_request():
    return request().model_copy(update={"model": "gpt-5.6-terra", "reasoning_effort": "high"})


def recording_client(monkeypatch, tmp_path, body=None, status=200):
    body = response_body() if body is None else body
    if status == 200:
        body.setdefault("parallel_tool_calls", False)
        body.setdefault("tool_choice", "none")
        body.setdefault("tools", [])
        if body.get("usage"):
            body["usage"]["input_tokens_details"].setdefault("cache_write_tokens", 0)
    client, calls, options = adapter(
        monkeypatch,
        lambda _: httpx.Response(
            status, json=body, headers={"x-request-id": "req_offline_fixture"}
        ),
    )
    experiment = Experiment(tmp_path / "offline-fixture", {"fixture": True}, resume=False)
    client.service_tier = "default"
    client.observer = experiment.record_provider_event
    return client, experiment, calls, options


def test_terra_high_real_sdk_wire_body_matches_budget_and_keeps_full_usage(monkeypatch, tmp_path):
    body = response_body()
    body["model"] = "gpt-5.6-terra"
    body["service_tier"] = "default"
    body["usage"]["input_tokens_details"] |= {"cached_tokens": 2, "cache_write_tokens": 1}
    body["output"].append(
        {
            "id": "rs_fixture",
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "private fixture reasoning"}],
        }
    )
    client, experiment, calls, options = recording_client(monkeypatch, tmp_path, body)
    req = terra_request()
    result = experiment.call(
        "fixture", "attribution", req, LLMResult, lambda: client.attribute(req), max_calls=1
    )
    wire = json.loads(calls[0].content)
    assert wire == request_body(req, max_output_tokens=2048)
    assert wire["reasoning"] == {"effort": "high"}
    assert options[0]["max_retries"] == 0
    assert result.usage.response_model == "gpt-5.6-terra"
    event = read_json(next((experiment.root / "calls").glob("*.provider.json")))
    assert event["response"]["usage"]["input_tokens_details"]["cache_write_tokens"] == 1
    assert event["request_id"] == "req_offline_fixture"
    assert event["omitted_reasoning_items"] == 1
    assert "private fixture reasoning" not in json.dumps(event)
    assert "offline_fake_key" not in json.dumps(event)
    assert event["started_at"] <= event["finished_at"]
    assert len(calls) == experiment.calls_started == 1


def test_visible_response_saved_before_schema_rejection(monkeypatch, tmp_path):
    body = response_body()
    body["output"][0]["content"][0]["text"] = '{"malformed":true}'
    client, experiment, calls, _ = recording_client(monkeypatch, tmp_path, body)
    with pytest.raises(RecordedCallFailure):
        experiment.call(
            "fixture",
            "attribution",
            terra_request(),
            LLMResult,
            lambda: client.attribute(terra_request()),
            max_calls=1,
        )
    event = read_json(next((experiment.root / "calls").glob("*.provider.json")))
    assert event["response"]["output"][0]["content"][0]["text"] == '{"malformed":true}'
    error = read_json(next((experiment.root / "calls").glob("*.error.json")))
    assert error["code"] == LLMOutputError.code
    assert error["usage"]["output_tokens"] == 30
    assert len(calls) == 1


def test_interruption_after_capture_recovers_without_second_http_request(monkeypatch, tmp_path):
    client, experiment, calls, _ = recording_client(monkeypatch, tmp_path)
    req = terra_request()

    def interrupted():
        client.attribute(req)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        experiment.call("fixture", "attribution", req, LLMResult, interrupted, max_calls=1)
    assert not list((experiment.root / "calls").glob("*.response.json"))
    resumed = Experiment(experiment.root, experiment.config, resume=True)
    prohibited = Mock(side_effect=AssertionError("A second paid call is forbidden"))
    recovered = resumed.call("fixture", "attribution", req, LLMResult, prohibited, max_calls=1)
    assert recovered.attribution.decision == "attributed"
    assert recovered.usage.response_id == "resp_test"
    assert len(calls) == resumed.calls_started == 1
    prohibited.assert_not_called()
    assert read_json(next((experiment.root / "calls").glob("*.recovery.json")))["paid_calls"] == 0


def test_http_failure_capture_never_persists_provider_message_or_authorization(
    monkeypatch, tmp_path
):
    client, experiment, calls, _ = recording_client(
        monkeypatch,
        tmp_path,
        {"error": {"message": "sk-do-not-retain", "type": "error", "code": "temporary"}},
        status=503,
    )

    def invoke():
        return client.attribute(terra_request())

    for _ in range(2):
        with pytest.raises(RecordedCallFailure):
            experiment.call(
                "fixture", "attribution", terra_request(), LLMResult, invoke, max_calls=2
            )
    captured = read_json(next((experiment.root / "calls").glob("*.transport.json")))
    assert captured["status_code"] == 503
    assert captured["request_id"] == "req_offline_fixture"
    saved = "".join(p.read_text(encoding="utf-8") for p in (experiment.root / "calls").glob("*"))
    assert "sk-do-not-retain" not in saved and "offline_fake_key" not in saved
    assert len(calls) == experiment.calls_started == 1


def test_recovery_accepts_sdk_field_names_in_echoed_structured_output_format(monkeypatch, tmp_path):
    body = response_body()
    body["text"] = {
        "format": request_body(terra_request(), max_output_tokens=2048)["text"]["format"]
    }
    client, experiment, calls, _ = recording_client(monkeypatch, tmp_path, body)
    req = terra_request()

    def interrupted():
        client.attribute(req)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        experiment.call("fixture", "attribution", req, LLMResult, interrupted, max_calls=1)
    event = read_json(next((experiment.root / "calls").glob("*.provider.json")))
    assert "schema_" in event["response"]["text"]["format"]
    resumed = Experiment(experiment.root, experiment.config, resume=True)
    prohibited = Mock(side_effect=AssertionError("Another HTTP request is forbidden"))
    recovered = resumed.call("fixture", "attribution", req, LLMResult, prohibited, max_calls=1)
    assert recovered.attribution.decision == "attributed"
    assert len(calls) == 1
    prohibited.assert_not_called()


def test_budget_reserves_reasoning_within_output_and_has_no_cache_discount():
    body = request_body(terra_request(), max_output_tokens=4096)
    bound = cost_bound(body, timeout_seconds=60)
    from decimal import Decimal

    expected = (
        Decimal(bound["input_token_planning_bound"]) * Decimal("2.50")
        + Decimal(4096) * Decimal("12")
    ) / 1000000
    assert Decimal(bound["planning_upper_cost_usd"]) == expected
    assert bound["max_output_tokens"] == 4096
