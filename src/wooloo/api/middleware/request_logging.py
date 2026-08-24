"""
Request correlation and access logging.

"""

import re
import time
import uuid
from collections.abc import Iterable
from typing import Final

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from wooloo.infrastructure.logging.logger import logger

_REQUEST_ID_HEADER: Final = b"x-request-id"

_SAFE_REQUEST_ID: Final = re.compile(r"[A-Za-z0-9._-]{1,128}")
"""
Pattern an inbound correlation ID must match in full to be trusted.

"""

_MAX_LOGGED_PATH: Final = 512
"""
Characters of a request path kept verbatim in a log record.

"""


class RequestLoggingMiddleware:
    """Correlate and log one HTTP request.

    Attributes:
        app: The next ASGI application in the stack.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the next application in the ASGI stack.

        Args:
            app: The application to delegate to.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI connection, correlating and logging HTTP ones.

        Args:
            scope: Connection metadata from the server.
            receive: Awaitable yielding inbound ASGI messages.
            send: Coroutine accepting outbound ASGI messages.

        Raises:
            Exception: Whatever the wrapped application raised, re-raised
                unchanged after being logged. Logging here is a side-effect, not
                handling: the server below still needs the exception to produce
                the actual 500 response.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _resolve_request_id(scope)
        scope.setdefault("state", {})["request_id"] = request_id
        status_code: int | None = None

        async def send_with_request_id(message: Message) -> None:
            """Stamp the correlation ID onto the response and record its status.

            Args:
                message: An outbound ASGI message, passed on unchanged unless it
                    starts the response.
            """
            nonlocal status_code

            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                headers.append((_REQUEST_ID_HEADER, request_id.encode("ascii")))
                message["headers"] = headers

            await send(message)

        tokens = structlog.contextvars.bind_contextvars(request_id=request_id)
        started_at = time.monotonic()
        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            if status_code is None:
                status_code = 500
            logger.exception(
                "http_request_failed",
                method=scope["method"],
                path=_truncate(scope["path"]),
                client_ip=_client_ip(scope),
            )
            raise
        finally:
            logger.info(
                "http_request",
                method=scope["method"],
                path=_truncate(scope["path"]),
                status_code=status_code,
                duration_ms=round((time.monotonic() - started_at) * 1000, 2),
                client_ip=_client_ip(scope),
            )
            structlog.contextvars.reset_contextvars(**tokens)


def _resolve_request_id(scope: Scope) -> str:
    """Return the request's correlation ID, reusing the client's when trustworthy.

    Args:
        scope: The HTTP scope whose ``X-Request-ID`` header is consulted.

    Returns:
        The inbound ID if it matches :data:`_SAFE_REQUEST_ID` in full, otherwise a
        freshly generated UUID4. An absent, empty, or malformed value is treated
        identically — the request gets a usable ID either way.
    """
    inbound = _read_header(scope, _REQUEST_ID_HEADER)

    if inbound is not None and _SAFE_REQUEST_ID.fullmatch(inbound):
        return inbound

    return str(uuid.uuid4())


def _truncate(value: str, limit: int = _MAX_LOGGED_PATH) -> str:
    """Bound an attacker-controlled field before it reaches the log pipeline.

    Args:
        value: The raw value to bound (e.g. a request path).
        limit: Maximum characters kept verbatim.

    Returns:
        ``value`` unchanged if within ``limit``; otherwise the first ``limit``
        characters followed by ``…[<original length>]`` so the record still
        signals that truncation happened and how large the original was.
    """
    return value if len(value) <= limit else f"{value[:limit]}…[{len(value)}]"


def _client_ip(scope: Scope) -> str | None:
    """Return the connecting client's IP address, if the ASGI server reports one.

    This middleware is the only source of per-request source-IP attribution, since
    uvicorn's own access log is disabled in favour of the ``http_request`` summary.

    Args:
        scope: The HTTP connection scope.

    Returns:
        The client host from ``scope["client"]``, or ``None`` when the ASGI server
        doesn't report one (some transports omit it).
    """
    client: tuple[str, int] | None = scope.get("client")

    return client[0] if client else None


def _read_header(scope: Scope, name: bytes) -> str | None:
    """Look up a single request header in an ASGI scope.

    Args:
        scope: The HTTP scope to read from.
        name: The lowercase header name to match.

    Returns:
        The first matching header's value, decoded as latin-1 (the codec HTTP
        headers are defined in, and the only one that cannot raise on arbitrary
        bytes), or ``None`` when the header is absent.
    """
    headers: Iterable[tuple[bytes, bytes]] = scope["headers"]

    for key, value in headers:
        if key.lower() == name:
            return value.decode("latin-1")

    return None
