"""
Unit tests for `MaxBodySizeMiddleware`.

"""

import json
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from wooloo.api.middleware.max_body_size import MaxBodySizeMiddleware, _BodyTooLarge
from wooloo.api.middleware.request_logging import RequestLoggingMiddleware

MAX_BYTES = 1024

CHUNK_SIZE = 256

REQUEST_ID = "req-123"


class BodyReadingApp:
    """An ASGI application that drains the request body, then answers `200`.

    Attributes:
        calls: How many times the application was entered, so a rejection that
            must happen *before* the application can be told apart from one that
            happens after.
        received: The body bytes the application actually saw, concatenated.
    """

    def __init__(self) -> None:
        """Start with no recorded calls."""
        self.calls = 0
        self.received = b""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Read the whole body, then send a trivial response.

        Args:
            scope: Connection metadata handed down by the middleware.
            receive: Awaitable yielding inbound ASGI messages.
            send: Coroutine accepting outbound ASGI messages.
        """
        self.calls += 1

        more_body = True
        while more_body:
            message = await receive()
            self.received += message.get("body", b"")
            more_body = bool(message.get("more_body", False))

        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


class StreamingApp:
    """An ASGI application that answers before it finishes reading.

    Models the one case the middleware cannot answer with a `413`: the status
    line is already on the wire by the time the ceiling is passed.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Open a response, then drain the body.

        Args:
            scope: Connection metadata handed down by the middleware.
            receive: Awaitable yielding inbound ASGI messages.
            send: Coroutine accepting outbound ASGI messages.
        """
        await send({"type": "http.response.start", "status": 200, "headers": []})

        more_body = True
        while more_body:
            message = await receive()
            more_body = bool(message.get("more_body", False))


def build_scope(
    *,
    content_length: bytes | None = None,
    request_id: str | None = None,
) -> Scope:
    """Build an HTTP ASGI scope of the shape a real server would produce.

    Args:
        content_length: Raw `Content-Length` value, or `None` to omit the header
            the way a chunked request does.
        request_id: Correlation ID to pre-seed into the scope state, as the
            request-logging middleware does when it runs outside this one.

    Returns:
        A scope accepted by the middleware without a server behind it.
    """
    headers: list[tuple[bytes, bytes]] = [(b"content-type", b"application/octet-stream")]

    if content_length is not None:
        headers.append((b"content-length", content_length))

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/storage/blobs",
        "raw_path": b"/api/v1/storage/blobs",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 51234),
        "server": ("testserver", 80),
    }

    if request_id is not None:
        scope["state"] = {"request_id": request_id}

    return scope


class ScriptedClient:
    """The inbound half of one request, delivered a chunk at a time.

    Attributes:
        calls: How many inbound messages the application asked for, which is what
            makes "rejected without reading the body" an assertable fact.
    """

    def __init__(self, body: bytes, chunk_size: int = CHUNK_SIZE) -> None:
        """Split a body into the chunks a server would deliver.

        Args:
            body: The full request body.
            chunk_size: Bytes per `http.request` message.
        """
        self._chunks = [body[at : at + chunk_size] for at in range(0, len(body), chunk_size)] or [
            b""
        ]
        self.calls = 0

    async def receive(self) -> Message:
        """Deliver the next inbound message.

        Returns:
            The next `http.request` message, then `http.disconnect` once the body
            is exhausted — the same sequence a server produces.
        """
        self.calls += 1

        if not self._chunks:
            return {"type": "http.disconnect"}

        chunk = self._chunks.pop(0)

        return {"type": "http.request", "body": chunk, "more_body": bool(self._chunks)}


async def drive(
    scope: Scope,
    *,
    body: bytes = b"",
    app: ASGIApp | None = None,
    max_bytes: int = MAX_BYTES,
) -> tuple[list[Message], ScriptedClient]:
    """Run one request through the middleware and collect what it sent.

    Args:
        scope: The connection to handle.
        body: The bytes the client sends.
        app: The application to wrap. Defaults to a fresh `BodyReadingApp`.
        max_bytes: The ceiling to enforce.

    Returns:
        Every ASGI message the middleware passed outward, and the client that fed
        it, so the number of inbound reads can be asserted.
    """
    sent: list[Message] = []
    client = ScriptedClient(body)

    async def send(message: Message) -> None:
        sent.append(message)

    middleware = MaxBodySizeMiddleware(app if app is not None else BodyReadingApp(), max_bytes)
    await middleware(scope, client.receive, send)

    return sent, client


def response_start(messages: list[Message]) -> Message:
    """Extract the message that opened the response.

    Args:
        messages: ASGI messages collected from a middleware call.

    Returns:
        The `http.response.start` message.

    Raises:
        AssertionError: If no response was started.
    """
    for message in messages:
        if message["type"] == "http.response.start":
            return message

    raise AssertionError("the middleware never started a response")


def response_body(messages: list[Message]) -> bytes:
    """Concatenate the response body from a captured exchange.

    Args:
        messages: ASGI messages collected from a middleware call.

    Returns:
        The body bytes, joined in order.
    """
    return b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )


def header_value(messages: list[Message], name: bytes) -> bytes | None:
    """Read one header off the started response.

    Args:
        messages: ASGI messages collected from a middleware call.
        name: The lowercase header name to match.

    Returns:
        The first matching value, or `None` when the header is absent.
    """
    for key, value in response_start(messages)["headers"]:
        if key.lower() == name:
            return bytes(value)

    return None


async def test_a_body_within_the_ceiling_reaches_the_application_untouched() -> None:
    """
    The cap must be invisible to every request that respects it.

    """
    payload = b"x" * (MAX_BYTES // 2)
    app = BodyReadingApp()

    sent, _ = await drive(
        build_scope(content_length=str(len(payload)).encode()), body=payload, app=app
    )

    assert response_start(sent)["status"] == 200
    assert app.received == payload


async def test_a_body_exactly_at_the_ceiling_is_accepted() -> None:
    """The limit is inclusive, so the boundary is not a silent off-by-one.

    A ceiling advertised as 5 GiB that refuses a 5 GiB layer would be a
    documentation bug that only shows up on the largest, slowest upload.
    """
    payload = b"x" * MAX_BYTES
    app = BodyReadingApp()

    sent, _ = await drive(
        build_scope(content_length=str(MAX_BYTES).encode()), body=payload, app=app
    )

    assert response_start(sent)["status"] == 200
    assert app.received == payload


async def test_an_oversized_declaration_is_rejected_without_reading_the_body() -> None:
    """
    A truthful `Content-Length` must be refused before a byte is accepted.

    """
    app = BodyReadingApp()

    sent, client = await drive(
        build_scope(content_length=b"999999999999"), body=b"small", app=app
    )

    assert response_start(sent)["status"] == 413
    assert app.calls == 0
    assert client.calls == 0


async def test_an_undeclared_body_over_the_ceiling_is_rejected() -> None:
    """
    A chunked request declares no size, so it must be counted as it arrives.

    """
    app = BodyReadingApp()

    sent, _ = await drive(build_scope(), body=b"x" * (MAX_BYTES * 4), app=app)

    assert response_start(sent)["status"] == 413
    assert len(app.received) <= MAX_BYTES + CHUNK_SIZE


async def test_a_body_that_outgrows_its_own_declaration_is_rejected() -> None:
    """
    A client that under-declares its body must not get through on the header alone.

    """
    app = BodyReadingApp()

    sent, _ = await drive(build_scope(content_length=b"10"), body=b"x" * (MAX_BYTES * 4), app=app)

    assert response_start(sent)["status"] == 413


@pytest.mark.parametrize("declared", [b"", b"abc", b"1e9", b"12 34", b"-1"], ids=repr)
async def test_a_malformed_content_length_falls_back_to_counting(declared: bytes) -> None:
    """An unparseable declaration must not be trusted, nor treated as a pass.

    Args:
        declared: A `Content-Length` a server might forward but this middleware
            cannot read as a size.
    """
    sent, _ = await drive(build_scope(content_length=declared), body=b"x" * (MAX_BYTES * 4))

    assert response_start(sent)["status"] == 413


async def test_the_rejection_carries_the_apis_standard_error_shape() -> None:
    """
    A client must be able to parse this failure with the parser it already has.

    """
    sent, _ = await drive(build_scope(content_length=b"999999999999", request_id=REQUEST_ID))

    assert header_value(sent, b"content-type") == b"application/json"
    assert header_value(sent, b"content-length") == str(len(response_body(sent))).encode()

    body: dict[str, Any] = json.loads(response_body(sent))
    assert set(body) == {"code", "message", "request_id"}
    assert body["code"] == "payload_too_large"
    assert body["request_id"] == REQUEST_ID


async def test_the_rejection_is_answerable_without_the_correlating_middleware() -> None:
    """
    The cap must still answer when it is mounted alone, e.g. in a test or a probe.

    """
    sent, _ = await drive(build_scope(content_length=b"999999999999"))

    assert json.loads(response_body(sent))["request_id"] is None


async def test_the_rejection_closes_the_connection() -> None:
    """
    A refused body is still in flight, so the connection must not be reused.

    """
    sent, _ = await drive(build_scope(content_length=b"999999999999"))

    assert header_value(sent, b"connection") == b"close"


async def test_an_overrun_after_the_response_started_is_left_to_kill_the_connection() -> None:
    """
    A `413` is impossible once a status is on the wire, so the request must die.

    """
    with pytest.raises(_BodyTooLarge):
        await drive(build_scope(), body=b"x" * (MAX_BYTES * 4), app=StreamingApp())


async def test_the_overrun_signal_survives_a_broad_application_except() -> None:
    """
    The overrun must not be catchable as an ordinary application error.
    
    """
    assert issubclass(_BodyTooLarge, BaseException)
    assert not issubclass(_BodyTooLarge, Exception)


@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
async def test_non_http_scopes_are_delegated_untouched(scope_type: str) -> None:
    """Only HTTP connections carry a request body to bound.

    Args:
        scope_type: An ASGI scope type this middleware must not interpret.
    """
    seen: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)

    async def receive() -> Message:
        return {"type": f"{scope_type}.startup"}

    async def send(message: Message) -> None:
        raise AssertionError("a non-HTTP scope must not be answered")

    await MaxBodySizeMiddleware(app, MAX_BYTES)({"type": scope_type}, receive, send)

    assert [scope["type"] for scope in seen] == [scope_type]


def test_a_rejection_is_correlated_and_logged_when_stacked_as_the_application_stacks_it() -> None:
    """
    A refused upload must still be a visible, traceable request.

    """
    stack = RequestLoggingMiddleware(MaxBodySizeMiddleware(BodyReadingApp(), MAX_BYTES))

    with structlog.testing.capture_logs() as events:
        response = TestClient(stack).post("/api/v1/storage/blobs", content=b"x" * (MAX_BYTES * 4))

    assert response.status_code == 413
    assert response.json()["request_id"] == response.headers["x-request-id"]

    summaries = [event for event in events if event["event"] == "http_request"]
    assert len(summaries) == 1
    assert summaries[0]["status_code"] == 413


def test_the_middleware_holds_no_cross_request_state() -> None:
    """
    One instance serves the whole process, so its only state is its configuration.

    """
    app = BodyReadingApp()

    attributes: dict[str, Any] = dict(vars(MaxBodySizeMiddleware(app, MAX_BYTES)))

    assert attributes == {"app": app, "max_bytes": MAX_BYTES}
