import json
import logging
import sys

from app.core.logging import JsonFormatter, bind_log_context, log_context


def test_structured_log_context_and_secret_redaction():
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "Password: s3cret", (), None)
    record.trace_payload = "do not log payload"
    with bind_log_context(request_id="request-1", run_id="run-1"):
        result = json.loads(JsonFormatter(("s3cret",)).format(record))
    assert result["request_id"] == "request-1"
    assert result["run_id"] == "run-1"
    assert result["message"] == "Password: [REDACTED]"
    assert "trace_payload" not in result
    assert not log_context.get()


def test_exception_logs_preserve_frames_without_payloads():
    try:
        raise RuntimeError("secret trace and SQL values")
    except RuntimeError:
        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "failure", (), sys.exc_info()
        )
    rendered = JsonFormatter().format(record)
    result = json.loads(rendered)
    assert result["exception_type"] == "RuntimeError"
    assert result["stack"][-1]["function"] == "test_exception_logs_preserve_frames_without_payloads"
    assert "secret trace" not in rendered
