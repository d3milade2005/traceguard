import logging
import re
from time import perf_counter
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import error_response, unexpected_error_handler
from app.core.logging import bind_log_context

logger = logging.getLogger(__name__)
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        supplied_id = Headers(scope=scope).get("x-request-id", "")
        request_id = supplied_id if REQUEST_ID_PATTERN.fullmatch(supplied_id) else uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        status = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        start = perf_counter()
        with bind_log_context(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_id)
            finally:
                logger.info(
                    "HTTP request completed",
                    extra={
                        "status": status,
                        "duration_ms": round((perf_counter() - start) * 1000, 2),
                    },
                )


class UnexpectedErrorMiddleware:
    """Translate unexpected failures inside CORS so error responses keep CORS headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def track_response(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, track_response)
        except Exception as exc:
            if started:
                logger.exception("Request failed after response started")
                raise
            response = await unexpected_error_handler(Request(scope), exc)
            await response(scope, receive, send)


class RequestBodyLimitMiddleware:
    """Bound both declared and streamed bodies before route parsing."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = Headers(scope=scope).get("content-length")
        if length is not None:
            try:
                declared_length = int(length)
                if declared_length < 0:
                    raise ValueError
            except ValueError:
                response = error_response(
                    scope["state"]["request_id"],
                    400,
                    "INVALID_CONTENT_LENGTH",
                    "Invalid body length.",
                )
                await response(scope, receive, send)
                return
            if declared_length > self.max_bytes:
                await self.reject(scope, receive, send)
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                await self.reject(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        delivered = False

        async def replay_body() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay_body, send)

    async def reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = error_response(
            scope["state"]["request_id"], 413, "PAYLOAD_TOO_LARGE", "The request body is too large."
        )
        await response(scope, receive, send)
