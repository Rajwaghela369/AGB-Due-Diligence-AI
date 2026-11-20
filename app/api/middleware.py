"""Request ids and one access-log line per request, bound into the JSON log context.

Pure ASGI rather than Starlette's `@middleware("http")`: the SSE stream must pass through
untouched, and the log line is written when the response body ends, so a long-running
stream reports its full duration. The query string is never logged, since it can carry
tokens.
"""

from __future__ import annotations

import json
import re
import time
import uuid

from core.logging import bind, get_logger
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.schemas import ErrorBody

log = get_logger("api.request")

# Starlette's ServerErrorMiddleware sits outside every user middleware, so its 500 response
# bypasses the header this layer adds. The id is also kept in the scope, which that outer
# layer shares, so the 500 handler can echo it.
REQUEST_ID_SCOPE_KEY = "agb.request_id"

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

        scope[REQUEST_ID_SCOPE_KEY] = request_id
        with bind(request_id=request_id, method=scope["method"], path=scope["path"]):
            try:
                await self.app(scope, receive, send_with_id)
            except BaseException:
                log.exception("request failed", extra={"status": 500})
                raise
            finally:
                duration_ms = round((time.perf_counter() - started) * 1000, 1)
                log.info(
                    "request finished",
                    extra={"status": status, "duration_ms": duration_ms},
                )


class RequestBodyTooLarge(Exception):
    """Raised inside the app when a streamed body crosses the limit; mapped to 413 by a handler."""


_TOO_LARGE_MESSAGE = "Die Datei überschreitet die zulässige Upload-Größe."


class BodyLimitMiddleware:
    """Refuse oversized request bodies before any parser buffers them.

    A declared Content-Length over the limit is answered here, without calling the app.
    A body without one (chunked) is counted as it streams through `receive`, and the
    app is stopped at the first chunk that crosses the limit. Multipart spooling and JSON
    parsing both read through `receive`, so neither can buffer more than the limit.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = dict(scope.get("headers", [])).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await _send_too_large(send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise RequestBodyTooLarge()
            return message

        await self.app(scope, limited_receive, send)


async def _send_too_large(send: Send) -> None:
    body = json.dumps(ErrorBody(code="too_large", message=_TOO_LARGE_MESSAGE).model_dump())
    payload = body.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})
