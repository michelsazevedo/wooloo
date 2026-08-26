"""
Request body size enforcement.

"""

import json
from collections.abc import Iterable
from typing import Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_CONTENT_LENGTH_HEADER: Final = b"content-length"

_TOO_LARGE_STATUS: Final = 413

_TOO_LARGE_CODE: Final = "payload_too_large"

_TOO_LARGE_MESSAGE: Final = "Request body exceeds the maximum accepted size"

_CLOSE_CONNECTION_HEADERS: Final = ((b"connection", b"close"),)
"""
Header ending the connection alongside the rejection.

"""


class _BodyTooLarge(BaseException):
    """Raised inside the wrapped ``receive`` when the body outgrows the ceiling.

    """


class MaxBodySizeMiddleware:
    """Refuse a request whose body exceeds a configured ceiling.

    Attributes:
        app: The next ASGI application in the stack.
        max_bytes: Largest request body accepted, in bytes.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        """Wrap the next application in the ASGI stack.

        Args:
            app: The application to delegate to.
            max_bytes: Largest request body to accept. Validated as positive where
                it is configured, in :class:`~wooloo.config.settings.Settings`.
        """
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI connection, bounding the body of HTTP ones.

        Args:
            scope: Connection metadata from the server.
            receive: Awaitable yielding inbound ASGI messages.
            send: Coroutine accepting outbound ASGI messages.

        Raises:
            _BodyTooLarge: If the ceiling is passed *after* the application has
                already put a status line on the wire — a streamed response that
                is still consuming its request body. No ``413`` can be sent at
                that point, so the exception is left to reach the server, which
                drops the connection. A truncated success would be the only other
                option, and is worse: the client would have no way to tell.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _declared_body_size(scope)

        if declared is not None and declared > self.max_bytes:
            await _reject(scope, send)
            return

        response_started = False

        async def send_tracking_response_start(message: Message) -> None:
            """Pass one outbound message on, noting whether it opened the response.

            Args:
                message: An outbound ASGI message, forwarded unchanged.
            """
            nonlocal response_started

            if message["type"] == "http.response.start":
                response_started = True

            await send(message)

        received = 0

        async def receive_counting_body() -> Message:
            """Yield one inbound message, failing once the body outgrows the ceiling.

            Returns:
                The next inbound message, unchanged.

            Raises:
                _BodyTooLarge: Once the body bytes seen so far exceed the ceiling.
                    Raised on the chunk that crosses it, so no more than one chunk
                    beyond the limit is ever held.
            """
            nonlocal received

            message = await receive()

            if message["type"] == "http.request":
                received += len(message.get("body", b""))

                if received > self.max_bytes:
                    raise _BodyTooLarge

            return message

        try:
            await self.app(scope, receive_counting_body, send_tracking_response_start)
        except _BodyTooLarge:
            if response_started:
                raise

            await _reject(scope, send)


def _declared_body_size(scope: Scope) -> int | None:
    """Read the body size the client claims it is sending.

    Args:
        scope: The HTTP scope whose ``Content-Length`` header is consulted.

    Returns:
        The declared size, or ``None`` when the header is absent (a chunked
        request) or unparseable. Either way the claim is only ever used to reject
        early: a request that makes none is still counted as it arrives, so a
        missing or malformed header buys a client nothing.
    """
    declared = _read_header(scope, _CONTENT_LENGTH_HEADER)

    if declared is None:
        return None

    try:
        return int(declared)
    except ValueError:
        return None


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


async def _reject(scope: Scope, send: Send) -> None:
    """Answer an oversized request with ``413`` and close the connection.

    Args:
        scope: The rejected request's scope, read for its correlation ID.
        send: Coroutine accepting outbound ASGI messages.
    """
    body = _rejection_body(scope)

    await send(
        {
            "type": "http.response.start",
            "status": _TOO_LARGE_STATUS,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                *_CLOSE_CONNECTION_HEADERS,
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _rejection_body(scope: Scope) -> bytes:
    """Render the rejection in the API's standard error shape.

    Args:
        scope: The rejected request's scope. Its correlation ID is read from the
            state the request-logging middleware puts there, defensively: this
            middleware must still answer if it is ever mounted without it.

    Returns:
        The JSON body, encoded as UTF-8.
    """
    request_id: str | None = scope.get("state", {}).get("request_id")

    return json.dumps(
        {
            "code": _TOO_LARGE_CODE,
            "message": _TOO_LARGE_MESSAGE,
            "request_id": request_id,
        }
    ).encode("utf-8")
