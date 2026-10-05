import json
import time
from uuid import uuid4

import structlog

logger = structlog.get_logger()


class RequestContextMiddleware:
    """Bound body buffering, same-origin writes, correlation IDs, and safe request logs."""

    def __init__(self, app, settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.monotonic()
        request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        headers = dict(scope.get("headers", []))
        origin = headers.get(b"origin", b"").decode()
        host = headers.get(b"host", b"").decode()
        allowed = set(self.settings.allowed_origins())
        allowed.add(f"{scope.get('scheme', 'http')}://{host}")

        async def reject(code, message, status):
            body = json.dumps(
                {"code": code, "message": message, "request_id": request_id, "retryable": False}
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"x-request-id", request_id.encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})

        if scope["method"] in {"POST", "PATCH", "PUT", "DELETE"} and origin and origin not in allowed:
            return await reject("origin_denied", "This request origin is not allowed.", 403)
        limit = self.settings.max_request_mb * 1024 * 1024
        try:
            if int(headers.get(b"content-length", b"0")) > limit:
                return await reject("request_too_large", "Request exceeds the configured size limit.", 413)
        except ValueError:
            return await reject("invalid_content_length", "Invalid request length.", 400)
        chunks = []
        size = 0
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            chunk = event.get("body", b"")
            size += len(chunk)
            if size > limit:
                return await reject("request_too_large", "Request exceeds the configured size limit.", 413)
            chunks.append(chunk)
            if not event.get("more_body", False):
                break
        body = b"".join(chunks)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        status = 500

        async def wrapped_send(event):
            nonlocal status
            if event["type"] == "http.response.start":
                status = event["status"]
                event["headers"] = list(event.get("headers", [])) + [
                    (b"x-request-id", request_id.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"cache-control", b"no-store"),
                ]
            await send(event)

        try:
            await self.app(scope, replay, wrapped_send)
        finally:
            logger.info(
                "http_request",
                request_id=request_id,
                method=scope["method"],
                endpoint=scope.get("route").path if scope.get("route") else scope["path"],
                status=status,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
