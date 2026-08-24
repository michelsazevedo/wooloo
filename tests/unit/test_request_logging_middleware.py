"""
Unit tests for `RequestLoggingMiddleware`.

"""

import uuid
from collections.abc import Iterable, Iterator
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from structlog.tracebacks import ExceptionDictTransformer
from structlog.typing import EventDict

from wooloo.api.middleware.request_logging import RequestLoggingMiddleware

WELL_FORMED_REQUEST_IDS = [
    "a",
    "9",
    "abc123",
    "req-123",
    "req_123",
    "req.123",
    "550e8400-e29b-41d4-a716-446655440000",
    "a" * 128,
]

MALFORMED_REQUEST_IDS = [
    "",
    " ",
    "has space",
    "bad\r\nInjected: yes",
    "bad\nInjected: yes",
    "bad\rInjected: yes",
    "a" * 129,
    "a;b",
    "a/b",
    "a:b",
    "<script>",
]


class StubApp:
    """A minimal ASGI application standing in for the routed application.

    Exists so the middleware has something to wrap; it is not a test double for
    anything under test. Records the scopes it receives so delegation can be
    asserted directly rather than inferred from a response.

    Attributes:
        status: The HTTP status to respond with.
        scopes: Every scope this application was called with, in order.
    """

    def __init__(self, status: int = 200) -> None:
        """Configure the response this application will produce.

        Args:
            status: The status code to send on `http.response.start`.
        """
        self.status = status
        self.scopes: list[Scope] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Record the scope and, for HTTP, send a trivial response.

        Args:
            scope: Connection metadata handed down by the middleware.
            receive: Awaitable yielding inbound ASGI messages. Unused.
            send: Coroutine accepting outbound ASGI messages.
        """
        self.scopes.append(scope)

        if scope["type"] != "http":
            return

        await send(
            {
                "type": "http.response.start",
                "status": self.status,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})


class ExplodingApp:
    """An ASGI application that fails the way a broken handler fails.

    Raises before sending anything, so the middleware never observes a status
    code — the state it must still log its summary line from.
    """

    failure = RuntimeError("handler blew up")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Raise unconditionally.

        Args:
            scope: Ignored.
            receive: Ignored.
            send: Ignored.

        Raises:
            RuntimeError: Always.
        """
        raise self.failure


class MidStreamExplodingApp:
    """An ASGI application that starts a response and then fails.

    Models the failure `ExplodingApp` cannot: a handler that has already put a
    status line on the wire — a streaming response whose generator dies partway
    through, a response followed by a raising background task — and only then
    raises. The middleware observes a status code *and* an exception for the same
    request, which is the state its 500-substitution guard exists to handle.

    Attributes:
        status: The status sent before the failure.
    """

    failure = RuntimeError("handler blew up after responding")

    status = 200

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Start a successful response, then raise.

        Args:
            scope: Ignored.
            receive: Ignored.
            send: Coroutine accepting outbound ASGI messages.

        Raises:
            RuntimeError: Always, after the response has started.
        """
        await send(
            {
                "type": "http.response.start",
                "status": self.status,
                "headers": [(b"content-type", b"text/plain")],
            }
        )

        raise self.failure


def build_scope(
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    method: str = "GET",
    path: str = "/raw",
) -> Scope:
    """Build an HTTP ASGI scope of the shape a real server would produce.

    Args:
        headers: Raw header pairs, lowercase names, as a server delivers them.
        method: The HTTP method to advertise.
        path: The request path to advertise.

    Returns:
        A scope accepted by the middleware without a server behind it.
    """
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": headers if headers is not None else [],
        "client": ("127.0.0.1", 51234),
        "server": ("testserver", 80),
    }


async def call_middleware(scope: Scope, app: ASGIApp | None = None) -> list[Message]:
    """Drive the middleware directly and collect what it sends.

    Bypasses the HTTP client entirely, which is what makes the raw outbound
    headers observable.

    Args:
        scope: The connection to handle.
        app: The application to wrap. Defaults to a fresh `StubApp`.

    Returns:
        Every ASGI message the middleware passed outward, in order.
    """
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await RequestLoggingMiddleware(app if app is not None else StubApp())(scope, receive, send)

    return sent


def response_headers(messages: Iterable[Message]) -> list[tuple[bytes, bytes]]:
    """Extract the raw header pairs from a captured response.

    Args:
        messages: ASGI messages collected from a middleware call.

    Returns:
        The headers carried on `http.response.start`, unparsed.

    Raises:
        AssertionError: If no response was started, which would mean the test
            was asserting on a response that never happened.
    """
    for message in messages:
        if message["type"] == "http.response.start":
            headers: list[tuple[bytes, bytes]] = list(message["headers"])
            return headers

    raise AssertionError("the middleware never started a response")


def http_request_events(events: Iterable[EventDict]) -> list[EventDict]:
    """Select the middleware's own summary events.

    Args:
        events: Everything captured during a test.

    Returns:
        Only the `http_request` events, in emission order.
    """
    return [event for event in events if event["event"] == "http_request"]


def assert_is_generated_uuid(value: str) -> None:
    """Assert a value is a freshly generated UUID4 rather than caller-supplied.

    Checks the version rather than merely that parsing succeeds: a client could
    supply a well-formed UUID1 — which encodes a MAC address and a timestamp —
    and a middleware that echoed it would pass a parse-only check while failing
    the actual contract, which is `uuid.uuid4()`.

    Args:
        value: The candidate correlation ID.
    """
    parsed = uuid.UUID(value)

    assert parsed.version == 4
    assert str(parsed) == value


@pytest.fixture
def stub_app() -> StubApp:
    """Return a fresh stub application, isolated per test."""
    return StubApp()


@pytest.fixture
def failure_logs() -> Iterator[list[EventDict]]:
    """Capture events with tracebacks rendered the way the application renders them.

    Yields:
        A list accumulating one event dict per log call, with `exc_info`
        replaced by a structured `exception` array on events that carry one.
    """
    with structlog.testing.capture_logs(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.ExceptionRenderer(ExceptionDictTransformer(show_locals=False)),
        ]
    ) as events:
        yield events


@pytest.fixture
def client(stub_app: StubApp) -> TestClient:
    """Return a client speaking to the middleware wrapped around the stub.

    Args:
        stub_app: The application the middleware will delegate to.
    """
    return TestClient(RequestLoggingMiddleware(stub_app))


def test_generates_a_request_id_when_the_client_supplies_none(
    client: TestClient, captured_logs: list[EventDict]
) -> None:
    """
    An uncorrelated request must still become traceable.

    """
    response = client.get("/anything")

    generated = response.headers["x-request-id"]
    assert_is_generated_uuid(generated)
    assert http_request_events(captured_logs)[0]["request_id"] == generated


def test_generates_a_distinct_request_id_for_every_request(client: TestClient) -> None:
    """
    Generated IDs must actually vary.

    """
    ids = {client.get("/anything").headers["x-request-id"] for _ in range(10)}

    assert len(ids) == 10


@pytest.mark.parametrize("inbound", WELL_FORMED_REQUEST_IDS, ids=repr)
def test_echoes_a_well_formed_inbound_request_id(
    client: TestClient, captured_logs: list[EventDict], inbound: str
) -> None:
    """
    A trusted caller's ID must survive verbatim, in the header and the log.

    """
    response = client.get("/anything", headers={"X-Request-ID": inbound})

    assert response.headers["x-request-id"] == inbound
    assert http_request_events(captured_logs)[0]["request_id"] == inbound


@pytest.mark.parametrize("inbound", MALFORMED_REQUEST_IDS, ids=repr)
def test_replaces_a_malformed_inbound_request_id(
    client: TestClient, captured_logs: list[EventDict], inbound: str
) -> None:
    """
    Untrusted input must be discarded, not sanitised and not propagated.

    """
    response = client.get("/anything", headers={"X-Request-ID": inbound})

    issued = response.headers["x-request-id"]
    assert issued != inbound
    assert_is_generated_uuid(issued)
    assert http_request_events(captured_logs)[0]["request_id"] == issued


@pytest.mark.parametrize(
    "inbound",
    [
        b"bad\r\nX-Injected: yes",
        b"bad\r\n\r\n<html>body</html>",
        b"bad\nX-Injected: yes",
        b"caf\xe9",
    ],
    ids=repr,
)
async def test_a_malformed_request_id_cannot_inject_a_response_header(inbound: bytes) -> None:
    """CRLF in an inbound ID must not become a second response header.

    Args:
        inbound: The raw header value bytes an attacker would send.
    """
    sent = await call_middleware(build_scope(headers=[(b"x-request-id", inbound)]))

    headers = response_headers(sent)
    correlation_headers = [value for name, value in headers if name.lower() == b"x-request-id"]

    assert len(correlation_headers) == 1
    assert_is_generated_uuid(correlation_headers[0].decode("ascii"))
    assert not any(b"\r" in part or b"\n" in part for pair in headers for part in pair)
    assert not any(b"injected" in name.lower() for name, _ in headers)


def test_preserves_the_headers_the_application_already_set(client: TestClient) -> None:
    """
    The correlation header must be added to the response, not substituted for it.

    """
    response = client.get("/anything")

    assert response.headers["content-type"] == "text/plain"
    assert response.headers["x-request-id"]


def test_emits_exactly_one_http_request_event_per_request(
    client: TestClient, captured_logs: list[EventDict]
) -> None:
    """
    One request must produce one summary line, carrying the full contract.

    """
    response = client.post("/api/v1/widgets")

    events = http_request_events(captured_logs)
    assert len(events) == 1

    event = events[0]
    assert event["log_level"] == "info"
    assert event["method"] == "POST"
    assert event["path"] == "/api/v1/widgets"
    assert event["status_code"] == 200
    assert event["request_id"] == response.headers["x-request-id"]


def test_http_request_reports_the_duration_as_rounded_milliseconds(
    client: TestClient, captured_logs: list[EventDict]
) -> None:
    """
    `duration_ms` must be a float already rounded to two decimals.

    """
    client.get("/anything")

    duration = http_request_events(captured_logs)[0]["duration_ms"]

    assert isinstance(duration, float)
    assert duration == round(duration, 2)
    assert duration >= 0.0


def test_http_request_reports_the_application_status_code(
    captured_logs: list[EventDict],
) -> None:
    """
    The logged status must come from the response, not from an assumption.

    """
    client = TestClient(RequestLoggingMiddleware(StubApp(status=503)))

    response = client.get("/anything")

    assert response.status_code == 503
    assert http_request_events(captured_logs)[0]["status_code"] == 503


async def test_logs_the_summary_even_when_the_application_raises(
    captured_logs: list[EventDict],
) -> None:
    """
    A crashing handler must not also cost us the record that it was called.

    """
    with pytest.raises(RuntimeError) as raised:
        await call_middleware(build_scope(path="/boom"), app=ExplodingApp())

    assert raised.value is ExplodingApp.failure

    events = http_request_events(captured_logs)
    assert len(events) == 1
    assert events[0]["path"] == "/boom"
    assert events[0]["status_code"] == 500
    assert_is_generated_uuid(events[0]["request_id"])


async def test_logs_the_traceback_once_and_correlated_when_the_application_raises(
    failure_logs: list[EventDict],
) -> None:
    """
    A crash must leave a traceback that is findable from the summary line.

    """
    with pytest.raises(RuntimeError):
        await call_middleware(build_scope(path="/boom"), app=ExplodingApp())

    failures = [event for event in failure_logs if event["event"] == "http_request_failed"]
    summaries = http_request_events(failure_logs)

    assert len(failures) == 1
    assert len(summaries) == 1

    failure = failures[0]
    assert failure["log_level"] == "error"
    assert failure["method"] == "GET"
    assert failure["path"] == "/boom"

    rendered = failure["exception"]
    assert isinstance(rendered, list)
    assert len(rendered) >= 1
    assert rendered[0]["exc_type"] == "RuntimeError"
    assert rendered[0]["exc_value"] == str(ExplodingApp.failure)
    assert rendered[0]["frames"]

    assert failure["request_id"] == summaries[0]["request_id"]


async def test_a_status_already_sent_to_the_client_survives_a_mid_stream_crash(
    captured_logs: list[EventDict],
) -> None:
    """
    A handler that crashes *after* responding must be logged with its real status.

    """
    with pytest.raises(RuntimeError) as raised:
        await call_middleware(build_scope(path="/mid-stream"), app=MidStreamExplodingApp())

    assert raised.value is MidStreamExplodingApp.failure

    events = http_request_events(captured_logs)
    assert len(events) == 1
    assert events[0]["path"] == "/mid-stream"
    assert events[0]["status_code"] == 200


async def test_clears_the_bound_context_when_the_application_raises() -> None:
    """
    A failed request must not leave its ID bound to the task.

    """
    with pytest.raises(RuntimeError):
        await call_middleware(build_scope(), app=ExplodingApp())

    assert structlog.contextvars.get_contextvars() == {}


def test_clears_the_bound_context_after_a_successful_request(client: TestClient) -> None:
    """Nothing may remain bound once the response is out the door."""
    client.get("/anything")

    assert structlog.contextvars.get_contextvars() == {}


@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
async def test_non_http_scopes_are_delegated_untouched(
    captured_logs: list[EventDict], scope_type: str
) -> None:
    """
    Only HTTP connections are correlated and logged.

    """
    stub = StubApp()
    scope: Scope = {"type": scope_type}
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": f"{scope_type}.startup"}

    async def send(message: Message) -> None:
        sent.append(message)

    await RequestLoggingMiddleware(stub)(scope, receive, send)

    assert [seen["type"] for seen in stub.scopes] == [scope_type]
    assert captured_logs == []
    assert structlog.contextvars.get_contextvars() == {}


def test_the_stub_application_receives_the_original_scope(
    client: TestClient, stub_app: StubApp
) -> None:
    """
    The middleware must not rewrite the request on its way down.

    """
    client.get("/api/v1/widgets", headers={"X-Request-ID": "abc123"})

    scope = stub_app.scopes[0]
    headers: list[tuple[bytes, bytes]] = list(scope["headers"])

    assert scope["path"] == "/api/v1/widgets"
    assert scope["method"] == "GET"
    assert (b"x-request-id", b"abc123") in headers


def test_the_middleware_holds_no_cross_request_state(stub_app: StubApp) -> None:
    """
    One middleware instance serves the whole process, so it must be stateless.

    """
    middleware = RequestLoggingMiddleware(stub_app)
    attributes: dict[str, Any] = dict(vars(middleware))

    assert attributes == {"app": stub_app}
