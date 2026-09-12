import json
from unittest.mock import Mock
from uuid import uuid4

import pytest
from kombu.exceptions import OperationalError

from app.core.exceptions import QueuePublishError, TraceAdapterError
from app.integrations.task_queue import CeleryRunPublisher
from app.integrations.traces.contracts import decode_record
from app.integrations.traces.sources import trace_adapters

SOURCE = "who_when.algorithm_generated"
MESSAGE = {"name": "Agent", "role": "assistant", "content": ""}


@pytest.mark.parametrize(
    ("body", "source", "status", "code"),
    [
        ({}, SOURCE, 422, "TRACE_ADAPTER_ERROR"),
        ({"question": "Q", "history": []}, SOURCE, 422, "TRACE_ADAPTER_ERROR"),
        ({"question": "Q", "history": [MESSAGE]}, "unknown", 422, "UNSUPPORTED_TRACE_SOURCE"),
        ({"question": "Q", "history": [{"content": ""}]}, SOURCE, 422, "TRACE_ADAPTER_ERROR"),
        (
            {"question": "Q", "history": [MESSAGE], "mistake_step": 0},
            SOURCE,
            422,
            "TRACE_ADAPTER_ERROR",
        ),
    ],
)
def test_invalid_submission_never_uses_database(client, body, source, status, code):
    response = client.post(f"/api/v1/runs?source={source}", json=body)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize("query", ["", "?source=" + SOURCE + "&mistake_agent=A"])
def test_missing_source_and_extra_query_fields_rejected(client, query):
    response = client.post("/api/v1/runs" + query, json={"question": "Q", "history": [MESSAGE]})
    assert response.status_code == 422


def test_ingestion_requires_json_content_type(client):
    response = client.post(f"/api/v1/runs?source={SOURCE}", content=b"{}")
    assert response.status_code == 415


@pytest.mark.parametrize(
    "raw", [b"", b"{", b"[]", b"null", b"\xff", b'{"question":1,"question":2}']
)
def test_malformed_record(raw):
    with pytest.raises(TraceAdapterError):
        decode_record(raw)


@pytest.mark.parametrize("variant", ["algorithm_generated", "hand_crafted"])
@pytest.mark.parametrize("string_history", [False, True])
@pytest.mark.parametrize(
    "label",
    [
        "question_ID",
        "ground_truth",
        "groundtruth",
        "is_correct",
        "is_corrected",
        "mistake_agent",
        "mistake_step",
        "mistake_reason",
        "mistake_type",
        "row_index",
    ],
)
def test_benchmark_fields_rejected_in_production(variant, string_history, label):
    adapter = trace_adapters()[f"who_when.{variant}"]
    message = dict(MESSAGE)
    if variant == "hand_crafted":
        del message["name"]
    history = [message]
    record = {"question": "Q", "history": json.dumps(history) if string_history else history}
    adapter.validate_submission(record)
    with pytest.raises(TraceAdapterError):
        adapter.validate_submission({**record, label: "label-secret"})
    message[label] = "label-secret"
    record["history"] = json.dumps(history) if string_history else history
    with pytest.raises(TraceAdapterError):
        adapter.validate_submission(record)


def test_publisher_sends_only_identifier_and_correlation():
    app = Mock()
    run_id = uuid4()
    CeleryRunPublisher(app, 3).publish(run_id, "request")
    args, kwargs = app.send_task.call_args
    assert args == ("traceguard.process_run",)
    assert kwargs["kwargs"] == {"run_id": str(run_id)}
    assert kwargs["headers"] == {"request_id": "request"}
    assert kwargs["confirm_timeout"] == 3


def test_publisher_translates_transport_failure():
    app = Mock()
    app.send_task.side_effect = OperationalError("amqp://secret")
    with pytest.raises(QueuePublishError) as failure:
        CeleryRunPublisher(app, 3).publish(uuid4(), None)
    assert "secret" not in str(failure.value)
