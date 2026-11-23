"""Portable, stdlib-only capture and HTTP helper for synchronous LangGraph nodes.

Copy this file into an external application. It imports no TraceGuard backend code.
Only instrumented application inputs/outputs are captured, never RunnableConfig.
"""

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID, uuid4

S = TypeVar("S")
SECRET_KEYS = frozenset(
    {
        "api_key",
        "openai_api_key",
        "password",
        "authorization",
        "access_token",
        "refresh_token",
        "client_secret",
        "cookie",
        "session_cookie",
    }
)


def snapshot(value: object) -> object:
    """Copy JSON data, visibly removing known structured credentials before submission."""
    copied = json.loads(json.dumps(value, allow_nan=False))

    def scrub(item: object) -> object:
        if isinstance(item, dict):
            removed = [key for key in item if key.lower() in SECRET_KEYS]
            result = {key: scrub(child) for key, child in item.items() if key not in removed}
            if removed:
                result["__traceguard_redacted_keys__"] = removed
            return result
        if isinstance(item, list):
            return [scrub(child) for child in item]
        return item

    return scrub(copied)


@dataclass(frozen=True)
class FailureSignal:
    kind: str
    message: str

    def __post_init__(self) -> None:
        if self.kind not in {"exception", "evaluator_failed", "timeout", "developer_report"}:
            raise ValueError("Unsupported failure signal")
        if not self.message.strip():
            raise ValueError("An explicit failure explanation is required")


class TraceRecorder:
    def __init__(self, task: str, *, execution_id: str | None = None) -> None:
        self.task = task
        self.execution_id = str(UUID(execution_id)) if execution_id else str(uuid4())
        self.events: list[dict[str, object]] = []
        self._lock = threading.Lock()

    def _invoke(
        self, agent: str, received: object, call: Callable[[], S], *, tool: str | None = None
    ) -> S:
        # Reserve an event before invocation, preserving observed start order even
        # when a node calls a tool or independent graph branches finish out of order.
        event: dict[str, object] = {
            "agent_name": agent,
            "event_type": "tool_call" if tool else "invocation",
            "input": snapshot(received),
            "status": "running",
        }
        if tool:
            event.update(tool_name=tool, tool_arguments=snapshot(received))
        with self._lock:
            event["started_at"] = datetime.now(UTC).isoformat()
            self.events.append(event)
        try:
            result = call()
            captured = snapshot(result)
        except Exception as exc:
            with self._lock:
                event.update(
                    status="error",
                    # Messages may contain arbitrary sensitive text; capture the
                    # exception class by default, not str(exc) or local variables.
                    error={"type": type(exc).__name__, "message": type(exc).__name__},
                    finished_at=datetime.now(UTC).isoformat(),
                )
            raise
        with self._lock:
            event.update(
                status="success", output=captured, finished_at=datetime.now(UTC).isoformat()
            )
            if tool:
                event["tool_result"] = captured
        return result

    def node(self, agent_name: str, function: Callable[[S], dict]) -> Callable[[S], dict]:
        def recorded(state: S) -> dict:
            return self._invoke(agent_name, state, lambda: function(state))

        return recorded

    def tool(self, agent_name: str, name: str, arguments: dict, function: Callable[[], S]) -> S:
        return self._invoke(agent_name, arguments, function, tool=name)

    def failed_trace(self, failure: FailureSignal | None) -> dict | None:
        if failure is None:
            return None
        with self._lock:
            if not self.events or any(event["status"] == "running" for event in self.events):
                raise ValueError("A completed captured execution is required")
            return {
                "version": "langgraph-v1",
                "execution_id": self.execution_id,
                "task": self.task,
                "failure": {"kind": failure.kind, "message": failure.message},
                "events": snapshot(self.events),
            }


class TraceGuardError(RuntimeError):
    def __init__(self, status: int | None = None, *, run_id: str | None = None) -> None:
        super().__init__(
            f"TraceGuard request failed (HTTP {status})."
            if status
            else "TraceGuard is unavailable or returned an invalid response."
        )
        self.status = status
        self.run_id = run_id


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TraceGuardClient:
    def __init__(
        self,
        base_url: str,
        session_cookie: str,
        *,
        origin: str,
        api_prefix: str = "/api/v1",
        request_timeout: float = 10,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or parsed.scheme == "http"
            and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        ):
            raise ValueError("Use an HTTPS server URL, or HTTP on loopback")
        if not session_cookie or any(c in session_cookie + origin for c in "\r\n"):
            raise ValueError("A session cookie and valid Origin header are required")
        if not api_prefix.startswith("/") or any(c in api_prefix for c in "?#\r\n"):
            raise ValueError("Invalid API prefix")
        if request_timeout <= 0:
            raise ValueError("A positive request timeout is required")
        self.base_url = base_url.rstrip("/")
        self.prefix = api_prefix.rstrip("/")
        self._cookie, self._origin = session_cookie, origin
        self.timeout = request_timeout
        self._opener = build_opener(NoRedirects())

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = (
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            if payload is not None
            else None
        )
        request = Request(
            self.base_url + self.prefix + path,
            data=body,
            method=method,
            headers={
                "Cookie": self._cookie,
                "Origin": self._origin,
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                data = response.read(1048577)
                if len(data) > 1048576:
                    raise TraceGuardError
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise TraceGuardError
                return value
        except HTTPError as exc:
            # Never put server bodies, credentials or trace data in exceptions/logs.
            raise TraceGuardError(exc.code) from None
        except (URLError, TimeoutError, ValueError, OSError):
            raise TraceGuardError from None

    def get_run(self, run_id: str) -> dict:
        return self._request("GET", f"/runs/{UUID(run_id)}")

    def get_analysis(self, run_id: str) -> dict:
        return self._request("GET", f"/runs/{UUID(run_id)}/analysis")

    def start_analysis(self, run_id: str, *, wait_timeout: float = 60) -> dict:
        deadline = time.monotonic() + wait_timeout
        while True:
            run = self.get_run(run_id)
            if run["status"] == "normalized":
                break
            if run["status"] == "failed":
                raise TraceGuardError(run_id=run_id)
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Normalization wait expired; resume Run {run_id}")
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        try:
            return self.get_analysis(run_id)
        except TraceGuardError as exc:
            if exc.status != 404:
                raise
        # Concurrent clients may both reach POST; the existing coordinator locks
        # its parent and owns one Analysis per Run. No replacement pipeline exists.
        return self._request("POST", f"/runs/{UUID(run_id)}/analysis")

    def submit_failed(
        self, trace: dict | None, *, auto_analyze: bool = False, wait_timeout: float = 60
    ) -> dict | None:
        if trace is None:
            return None
        if not isinstance(trace.get("failure"), dict):
            raise ValueError("An explicit failure signal is required")
        result = self._request("POST", "/runs?source=langgraph", trace)
        if result["status"] == "failed":
            raise TraceGuardError(run_id=result["id"])
        if auto_analyze:
            result["analysis"] = self.start_analysis(result["id"], wait_timeout=wait_timeout)
        return result
