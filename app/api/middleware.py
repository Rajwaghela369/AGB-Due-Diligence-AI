"""Request ids and one access-log line per request, bound into the JSON log context.

Pure ASGI rather than Starlette's `@middleware("http")`: the SSE stream must pass through
untouched, and the log line is written when the response body ends, so a long-running
stream reports its full duration. The query string is never logged, since it can carry
tokens.
"""

from __future__ import annotations

import re
import time
import uuid

from core.logging import bind, get_logger
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = get_logger("api.request")

# A caller may supply its own id (for tracing across a proxy); anything else is replaced.
_INCOMING_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class RequestLogMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope.get("headers", [])).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _INCOMING_ID.match(incoming) else uuid.uuid4().hex
        status = 500
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-Id"] = request_id
            await send(message)

        with bind(request_id=request_id, method=scope["method"], path=scope["path"]):
            try:
                await self.app(scope, receive, send_with_id)
            finally:
                duration_ms = round((time.perf_counter() - started) * 1000, 1)
                log.info(
                    "request finished",
                    extra={"status": status, "duration_ms": duration_ms},
                )
