"""Server-controlled request identity, body limits, headers, and safe access logs."""

import json
import logging
import time
import uuid

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings, normalize_trusted_host
from app.core.logging import request_id_context
from app.core.network import resolve_client_ip

logger = logging.getLogger("sou2ai.request")


class RequestSecurityMiddleware:
    """Apply controls that must wrap every HTTP response."""

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = str(uuid.uuid4())
        context_token = request_id_context.set(request_id)
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        request = Request(scope)
        state["client_ip"] = resolve_client_ip(request, self.settings)
        started = time.perf_counter()

        if not self._host_is_allowed(request.headers.get("host", "")):
            await self._send_error(
                send,
                request_id=request_id,
                status_code=400,
                error_code="invalid_host",
                message="The Host header is not allowed.",
            )
            self._log(scope, 400, started, request_id, state["client_ip"])
            request_id_context.reset(context_token)
            return

        expects_body = scope.get("method") in {"POST", "PATCH", "DELETE"} or any(
            name in {b"content-length", b"transfer-encoding"}
            for name, _value in scope.get("headers", [])
        )
        body_messages, too_large = (
            await self._read_limited_body(scope, receive)
            if expects_body
            else ([], False)
        )
        if too_large:
            await self._send_error(
                send,
                request_id=request_id,
                status_code=413,
                error_code="request_body_too_large",
                message="Request body exceeds the allowed size.",
            )
            self._log(scope, 413, started, request_id, state["client_ip"])
            request_id_context.reset(context_token)
            return

        status_code = 500

        async def replay_receive() -> Message:
            if body_messages:
                return body_messages.pop(0)
            if expects_body:
                return {"type": "http.request", "body": b"", "more_body": False}
            return await receive()

        async def secured_send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = list(message.get("headers", []))
                self._set_header(headers, b"x-request-id", request_id.encode("ascii"))
                self._set_header(headers, b"x-content-type-options", b"nosniff")
                self._set_header(headers, b"x-frame-options", b"DENY")
                self._set_header(headers, b"referrer-policy", b"no-referrer")
                self._set_header(headers, b"cache-control", b"no-store")
                if self.settings.hsts_enabled:
                    self._set_header(
                        headers,
                        b"strict-transport-security",
                        b"max-age=31536000; includeSubDomains",
                    )
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, replay_receive, secured_send)
        finally:
            self._log(scope, status_code, started, request_id, state["client_ip"])
            request_id_context.reset(context_token)

    async def _read_limited_body(
        self, scope: Scope, receive: Receive
    ) -> tuple[list[Message], bool]:
        content_length = dict(scope.get("headers", [])).get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.settings.max_request_body_bytes:
                    return [], True
            except ValueError:
                pass

        messages: list[Message] = []
        size = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                return messages, False
            if message["type"] != "http.request":
                continue
            size += len(message.get("body", b""))
            if size > self.settings.max_request_body_bytes:
                return [], True
            if not message.get("more_body", False):
                return messages, False

    def _host_is_allowed(self, raw_host: str) -> bool:
        if not raw_host:
            return False
        host_value = raw_host.strip()
        if host_value.startswith("["):
            closing = host_value.find("]")
            if closing < 0:
                return False
            hostname = host_value[1:closing]
            suffix = host_value[closing + 1 :]
            if suffix:
                if not suffix.startswith(":") or not suffix[1:].isdigit():
                    return False
                if not 1 <= int(suffix[1:]) <= 65_535:
                    return False
        else:
            hostname = host_value
            if host_value.count(":") == 1:
                candidate, separator, port = host_value.rpartition(":")
                if separator and port.isdigit():
                    if not 1 <= int(port) <= 65_535:
                        return False
                    hostname = candidate
                elif separator:
                    return False
        try:
            hostname = normalize_trusted_host(hostname)
        except ValueError:
            return False
        if hostname == "*" or hostname.startswith("*."):
            return False
        for pattern in self.settings.trusted_hosts:
            if pattern == "*" or hostname == pattern:
                return True
            if pattern.startswith("*.") and hostname.endswith(pattern[1:]):
                return True
        return False

    async def _send_error(
        self,
        send: Send,
        *,
        request_id: str,
        status_code: int,
        error_code: str,
        message: str,
    ) -> None:
        content = json.dumps(
            {
                "error": {
                    "code": error_code,
                    "message": message,
                    "request_id": request_id,
                }
            },
            separators=(",", ":"),
        ).encode("utf-8")
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(content)).encode("ascii")),
            (b"x-request-id", request_id.encode("ascii")),
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"no-referrer"),
            (b"cache-control", b"no-store"),
        ]
        await send(
            {"type": "http.response.start", "status": status_code, "headers": headers}
        )
        await send({"type": "http.response.body", "body": content})

    @staticmethod
    def _set_header(
        headers: list[tuple[bytes, bytes]], name: bytes, value: bytes
    ) -> None:
        headers[:] = [(key, item) for key, item in headers if key.lower() != name]
        headers.append((name, value))

    @staticmethod
    def _log(
        scope: Scope,
        status_code: int,
        started: float,
        request_id: str,
        client_ip: str,
    ) -> None:
        route = scope.get("route")
        route_template = getattr(route, "path", "unmatched")
        logger.info(
            "request_completed",
            extra={
                "event": "request_completed",
                "request_id": request_id,
                "http_method": scope.get("method", ""),
                "route_template": route_template,
                "status_code": status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "client_ip": client_ip,
            },
        )
