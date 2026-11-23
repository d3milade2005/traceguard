import xml.etree.ElementTree as ET
from typing import TypedDict

from app.schemas.validations import ExecutionResult, Variant


class ResultBase(TypedDict):
    variant: Variant
    container_id: str
    duration_ms: int
    exit_code: int
    output_truncated: bool


def parse_result(
    *,
    variant: Variant,
    container_id: str,
    duration_ms: int,
    exit_code: int,
    junit: str | None,
    test_name: str,
    max_result_bytes: int,
    timed_out: bool = False,
    oom: bool = False,
    output_truncated: bool = False,
) -> ExecutionResult:
    base: ResultBase = dict(
        variant=variant,
        container_id=container_id,
        duration_ms=duration_ms,
        exit_code=exit_code,
        output_truncated=output_truncated,
    )
    if timed_out:
        return ExecutionResult(**base, outcome="timeout", timed_out=True, diagnostic="TIMEOUT")
    if oom or exit_code in {-9, 137} and not output_truncated:
        return ExecutionResult(
            **base, outcome="resource_limit", resource_limited=True, diagnostic="RESOURCE_LIMIT"
        )
    if output_truncated:
        return ExecutionResult(**base, outcome="execution_error", diagnostic="OUTPUT_LIMIT")
    try:
        if junit is None or len(junit.encode()) > max_result_bytes or "<!" in junit:
            raise ValueError("Invalid bounded result")
        root = ET.fromstring(junit)
        cases = list(root.iter("testcase"))
        if len(cases) != 1 or cases[0].get("name") != test_name:
            raise ValueError("Expected exactly one test")
        case = cases[0]
        failed, errors = len(case.findall("failure")), len(case.findall("error"))
        if failed > 1 or errors > 1 or case.find("skipped") is not None:
            raise ValueError("Unexpected PyTest result")
        properties = case.findall("./properties/property")
        marker = any(
            p.get("name") == "traceguard_assertion" and p.get("value") == "true" for p in properties
        )
        if exit_code == 0 and not failed and not errors:
            return ExecutionResult(**base, outcome="passed", passed=1, diagnostic="PASS")
        if exit_code == 1 and failed == 1 and not errors and marker:
            return ExecutionResult(
                **base,
                outcome="assertion_failed",
                failed=1,
                diagnostic="REGRESSION_ASSERTION_FAILED",
            )
        return ExecutionResult(
            **base,
            outcome="execution_error",
            failed=failed,
            errors=errors,
            diagnostic="EXECUTION_ERROR",
        )
    except (ValueError, ET.ParseError):
        return ExecutionResult(**base, outcome="execution_error", diagnostic="RESULT_INVALID")
