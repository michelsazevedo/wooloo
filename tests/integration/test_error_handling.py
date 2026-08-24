"""
Integration tests for the API's error-handling composition (US-010).

"""

from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass
from typing import Any, Final

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import ASGIApp
from structlog.tracebacks import ExceptionDictTransformer
from structlog.typing import EventDict

from wooloo.api.errors.exceptions import (
    ConflictException,
    NotFoundException,
    ValidationException,
    WoolooException,
)
from wooloo.api.errors.handlers import (
    unhandled_exception_handler,
    wooloo_exception_handler,
)
from wooloo.api.middleware.request_logging import RequestLoggingMiddleware

CUSTOM_NOT_FOUND_MESSAGE: Final = "Widget 42 not found"
CUSTOM_CONFLICT_MESSAGE: Final = "Widget 42 already exists"

INTERNAL_DETAIL: Final = "internal secret detail xyz"
"""
The text a genuine bug carries, which must reach the log and never the response.

Chosen to be a string no legitimate part of a 500 body could contain, so
`INTERNAL_DETAIL not in response.text` is a meaningful assertion rather than one
that passes by coincidence.

"""

GENERIC_500_MESSAGE: Final = "An unexpected error occurred"

SUPPLIED_REQUEST_ID: Final = "client-supplied-id"

FAILURE_EVENT: Final = "request_failed"
"""
The event both exception handlers emit — the layer nearest the failure.

"""

MIDDLEWARE_FAILURE_EVENT: Final = "http_request_failed"
"""
The event `RequestLoggingMiddleware` emits when an exception escapes the app.

"""

SUMMARY_EVENT: Final = "http_request"


@dataclass(frozen=True)
class _DomainCase:
    """One deliberately raised exception and the HTTP answer it must produce.

    Attributes:
        label: Identifier used for the pytest parameter id.
        path: The throwaway route that raises this case's exception.
        build: Constructs the exception to raise. A bare class reference means
            "raised with no custom message", exercising the handler's default
            wording; a lambda passing text exercises the override.
        status_code: HTTP status the response must carry.
        code: Machine-readable `code` the body must carry.
        message: Exact `message` the body must carry.
    """

    label: str

    path: str

    build: Callable[[], WoolooException]

    status_code: int

    code: str

    message: str


DOMAIN_CASES: Final[tuple[_DomainCase, ...]] = (
    _DomainCase(
        label="validation-default",
        path="/raise/validation",
        build=ValidationException,
        status_code=400,
        code="validation_error",
        message="Validation failed",
    ),
    _DomainCase(
        label="not-found-default",
        path="/raise/not-found",
        build=NotFoundException,
        status_code=404,
        code="not_found",
        message="Resource not found",
    ),
    _DomainCase(
        label="conflict-default",
        path="/raise/conflict",
        build=ConflictException,
        status_code=409,
        code="conflict",
        message="Resource already exists",
    ),
    _DomainCase(
        label="not-found-custom-message",
        path="/raise/not-found-custom",
        build=lambda: NotFoundException(CUSTOM_NOT_FOUND_MESSAGE),
        status_code=404,
        code="not_found",
        message=CUSTOM_NOT_FOUND_MESSAGE,
    ),
    _DomainCase(
        label="conflict-custom-message",
        path="/raise/conflict-custom",
        build=lambda: ConflictException(CUSTOM_CONFLICT_MESSAGE),
        status_code=409,
        code="conflict",
        message=CUSTOM_CONFLICT_MESSAGE,
    ),
)
"""
Every anticipated failure, with and without caller-supplied wording.

The custom-message rows exist because the default rows alone cannot tell a
working override from a handler that ignores `exc.message` entirely and always
renders its own default text — both would be green.

"""

NOT_FOUND_CASE: Final = DOMAIN_CASES[1]
"""
The representative domain case for assertions that need only one of them.

"""

UNHANDLED_PATH: Final = "/raise/unhandled"

CORRELATED_PATHS: Final[tuple[str, ...]] = (
    *(case.path for case in DOMAIN_CASES),
    UNHANDLED_PATH,
)
"""
Every path whose response must carry a correlation ID — 5xx included.

"""


def _raising_endpoint(
    build: Callable[[], Exception],
) -> Callable[[], Coroutine[Any, Any, None]]:
    """Build a route handler whose only behaviour is to raise.

    Args:
        build: Constructs the exception to raise, called per request so no two
            requests share an exception instance.

    Returns:
        An async endpoint that always raises and therefore never returns. It is
        annotated `-> None` rather than `-> NoReturn` because FastAPI reads the
        return annotation to build a response model, and `None` is the shape
        that tells it there is nothing to serialise.
    """

    async def endpoint() -> None:
        raise build()

    return endpoint


def _build_error_app() -> ASGIApp:
    """Assemble a minimal clone of `main.py`'s error-handling composition.

    Returns:
        The wrapped ASGI application these tests drive requests through.
    """
    app = FastAPI()

    app.add_exception_handler(WoolooException, wooloo_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)

    for case in DOMAIN_CASES:
        app.add_api_route(case.path, _raising_endpoint(case.build), methods=["GET"])

    app.add_api_route(
        UNHANDLED_PATH,
        _raising_endpoint(lambda: RuntimeError(INTERNAL_DETAIL)),
        methods=["GET"],
    )

    return RequestLoggingMiddleware(app)


ERROR_APP: Final = _build_error_app()
"""
Built once at import: it is constructed from constants and never mutated.

Unlike `wooloo.main.app`, nothing here installs `dependency_overrides` or any
other per-test state, so there is no shared mutable surface for one test to leave
behind for the next. Each test still gets its own `TestClient`.

"""


@pytest.fixture
def client() -> TestClient:
    """Return a client for the locally composed error-handling stack.

    See the module docstring for why `raise_server_exceptions` is disabled: the
    500 response is the artefact under test, and the default behaviour would
    throw it away and re-raise instead.
    """
    return TestClient(ERROR_APP, raise_server_exceptions=False)


@pytest.fixture
def captured_logs_with_tracebacks() -> Iterator[list[EventDict]]:
    """Collect structlog events with exceptions rendered as structured data.

    Yields:
        A list accumulating one event dict per log call, in emission order.
    """
    with structlog.testing.capture_logs(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.ExceptionRenderer(ExceptionDictTransformer(show_locals=False)),
        ]
    ) as events:
        yield events


def events_named(events: list[EventDict], name: str) -> list[EventDict]:
    """Select captured events by event name.

    Args:
        events: Everything captured during a test.
        name: The structlog event name to match.

    Returns:
        The matching events, in emission order.
    """
    return [event for event in events if event["event"] == name]


def single_event(events: list[EventDict], name: str) -> EventDict:
    """Return the one event with the given name, asserting there is exactly one.

    Args:
        events: Everything captured during a test.
        name: The structlog event name to match.

    Returns:
        The single matching event.
    """
    matches = events_named(events, name)
    assert len(matches) == 1, f"expected exactly one {name!r} event, got {len(matches)}"

    return matches[0]


@pytest.mark.parametrize("case", DOMAIN_CASES, ids=lambda case: case.label)
def test_a_domain_exception_maps_to_its_status_code_and_error_body(
    client: TestClient, case: _DomainCase
) -> None:
    """Each anticipated failure must reach the client as its mapped HTTP answer.

    Args:
        case: The exception to raise and the response it must produce.
    """
    response = client.get(case.path)

    assert response.status_code == case.status_code
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "code": case.code,
        "message": case.message,
        "request_id": response.headers["x-request-id"],
    }


def test_an_unhandled_exception_maps_to_an_opaque_internal_error(client: TestClient) -> None:
    """
    A bug must reach the client as a 500 that says nothing about the bug.

    """
    response = client.get(UNHANDLED_PATH)

    assert response.status_code == 500
    assert response.json() == {
        "code": "internal_error",
        "message": GENERIC_500_MESSAGE,
        "request_id": response.headers["x-request-id"],
    }


@pytest.mark.parametrize("path", CORRELATED_PATHS)
@pytest.mark.parametrize("inbound", [None, SUPPLIED_REQUEST_ID], ids=["generated", "supplied"])
def test_the_error_body_request_id_matches_the_response_header(
    client: TestClient, path: str, inbound: str | None
) -> None:
    """Every error response must be correlated, in the body and in the header.

    Args:
        path: The failing route to request.
        inbound: The `X-Request-ID` to send, or `None` to send no header.
    """
    headers = {} if inbound is None else {"X-Request-ID": inbound}

    response = client.get(path, headers=headers)

    issued = response.headers["x-request-id"]
    assert issued
    assert response.json()["request_id"] == issued

    if inbound is None:
        assert issued != SUPPLIED_REQUEST_ID
    else:
        assert issued == inbound


@pytest.mark.parametrize("case", DOMAIN_CASES, ids=lambda case: case.label)
def test_a_domain_exception_logs_a_warning_with_no_traceback(
    client: TestClient,
    captured_logs_with_tracebacks: list[EventDict],
    case: _DomainCase,
) -> None:
    """An anticipated failure must be a warning, and must carry no traceback.

    Args:
        case: The exception to raise and the code its log line must carry.
    """
    response = client.get(case.path)

    record = single_event(captured_logs_with_tracebacks, FAILURE_EVENT)
    assert record["log_level"] == "warning"
    assert record["code"] == case.code
    assert record["request_id"] == response.headers["x-request-id"]
    assert "exception" not in record
    assert "exc_info" not in record


def test_an_unhandled_exception_logs_an_error_with_a_structured_traceback(
    client: TestClient, captured_logs_with_tracebacks: list[EventDict]
) -> None:
    """
    A bug must be logged loudly, with a traceback that is data, not prose.

    """
    response = client.get(UNHANDLED_PATH)

    record = single_event(captured_logs_with_tracebacks, FAILURE_EVENT)
    assert record["log_level"] == "error"
    assert record["code"] == "internal_error"
    assert record["request_id"] == response.headers["x-request-id"]

    rendered = record["exception"]
    assert isinstance(rendered, list)
    assert len(rendered) >= 1
    assert rendered[0]["exc_type"] == "RuntimeError"
    assert rendered[0]["exc_value"] == INTERNAL_DETAIL

    frames = rendered[0]["frames"]
    assert frames
    assert all(set(frame) == {"filename", "lineno", "name"} for frame in frames)


def test_a_domain_exception_produces_exactly_one_failure_record(
    client: TestClient, captured_logs_with_tracebacks: list[EventDict]
) -> None:
    """
    An anticipated failure is logged once, by the layer that answered it.

    """
    response = client.get(NOT_FOUND_CASE.path)

    assert response.status_code == 404
    assert [event["event"] for event in captured_logs_with_tracebacks] == [
        FAILURE_EVENT,
        SUMMARY_EVENT,
    ]
    assert not events_named(captured_logs_with_tracebacks, MIDDLEWARE_FAILURE_EVENT)

    summary = single_event(captured_logs_with_tracebacks, SUMMARY_EVENT)
    assert summary["status_code"] == 404
    assert summary["request_id"] == response.headers["x-request-id"]


def test_an_unhandled_exception_produces_two_independent_failure_records(
    client: TestClient, captured_logs_with_tracebacks: list[EventDict]
) -> None:
    """
    One 500 is logged twice, by two layers, and that is the intended design.

    """
    response = client.get(UNHANDLED_PATH)
    issued = response.headers["x-request-id"]

    assert response.status_code == 500
    assert [event["event"] for event in captured_logs_with_tracebacks] == [
        FAILURE_EVENT,
        MIDDLEWARE_FAILURE_EVENT,
        SUMMARY_EVENT,
    ]
    assert all(event["request_id"] == issued for event in captured_logs_with_tracebacks)

    summary = single_event(captured_logs_with_tracebacks, SUMMARY_EVENT)
    assert summary["status_code"] == 500


def test_the_internal_error_response_never_leaks_the_exception_detail(
    client: TestClient, captured_logs_with_tracebacks: list[EventDict]
) -> None:
    """
    The 500 body must disclose nothing the exception said, only the log may.

    """
    response = client.get(UNHANDLED_PATH)

    assert INTERNAL_DETAIL not in response.text
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text
    assert response.json() == {
        "code": "internal_error",
        "message": GENERIC_500_MESSAGE,
        "request_id": response.headers["x-request-id"],
    }

    record = single_event(captured_logs_with_tracebacks, FAILURE_EVENT)
    assert record["exception"][0]["exc_value"] == INTERNAL_DETAIL


def test_the_error_stack_does_not_leak_correlation_between_requests(
    client: TestClient, captured_logs_with_tracebacks: list[EventDict]
) -> None:
    """
    Correlation must be per-request on the failure path too.

    """
    first = client.get(UNHANDLED_PATH, headers={"X-Request-ID": SUPPLIED_REQUEST_ID})
    second = client.get(NOT_FOUND_CASE.path)

    second_id = second.headers["x-request-id"]
    assert first.headers["x-request-id"] == SUPPLIED_REQUEST_ID
    assert second_id != SUPPLIED_REQUEST_ID

    later_events = captured_logs_with_tracebacks[3:]
    assert [event["event"] for event in later_events] == [FAILURE_EVENT, SUMMARY_EVENT]
    assert all(event["request_id"] == second_id for event in later_events)
