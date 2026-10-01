import json
import logging
import sys

from app.core.logging import JsonFormatter, bind_log_context, configure_logging, log_context


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


def test_attribution_logs_identifiers_and_usage_only():
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "Response received", (), None)
    record.input_tokens = 100
    record.reasoning_tokens = 20
    record.instructions = "private prompt"
    record.evidence_json = "private trace"
    with bind_log_context(attribution_id="attribution-1", model="gpt-5.6-luna"):
        result = json.loads(JsonFormatter().format(record))
    assert result["attribution_id"] == "attribution-1"
    assert result["input_tokens"] == 100
    assert "instructions" not in result and "evidence_json" not in result


def test_sdk_debug_logs_are_suppressed_and_key_redacted(settings):
    from pydantic import SecretStr

    configure_logging(settings.model_copy(update={"openai_api_key": SecretStr("unit-secret")}))
    assert logging.getLogger("openai").level == logging.WARNING
    assert logging.getLogger("httpx").level == logging.WARNING
    formatter = logging.getLogger().handlers[0].formatter
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "unit-secret", (), None)
    assert "unit-secret" not in formatter.format(record)
