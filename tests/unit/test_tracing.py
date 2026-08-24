"""
Tests for the tracing wiring: provider registration, HTTP spans, manual spans.

"""

from collections.abc import AsyncIterator, Iterator
from importlib.metadata import version
from typing import Final, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.semconv.resource import ResourceAttributes
from opentelemetry.trace import SpanKind
from sqlalchemy.ext.asyncio import AsyncSession

from wooloo.config.settings import get_settings
from wooloo.infrastructure.database.engine import get_db_session
from wooloo.infrastructure.telemetry import tracing
from wooloo.infrastructure.telemetry.config import get_tracer
from wooloo.infrastructure.telemetry.tracing import configure_tracing
from wooloo.main import app

pytestmark = pytest.mark.usefixtures("pristine_tracer_provider")

HEALTHZ_URL: Final = "/api/v1/healthz"

SERVICE_NAME: Final = "wooloo"
"""Identity every span produced by this process must carry."""

DISTRIBUTION_NAME: Final = "wooloo"
"""Distribution whose installed metadata supplies the expected ``service.version``."""

MANUAL_SPAN_NAME: Final = "test_span"

HEALTHZ_SPAN_NAME: Final = "GET /api/v1/healthz"
"""
Name the instrumentation gives the request span.

Built from the *route template*, not the request path. Asserted because that is
the difference between a backend that can aggregate "how slow is healthz" and one
drowning in a distinct span name per concrete URL — the cardinality trap
instrumentation exists to avoid.

"""

HTTP_METHOD_ATTRIBUTE: Final = "http.method"
HTTP_ROUTE_ATTRIBUTE: Final = "http.route"
HTTP_STATUS_CODE_ATTRIBUTE: Final = "http.status_code"


def install_in_memory_exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Point the tracing module's only export seam at an in-memory exporter.

    Args:
        monkeypatch: Used to substitute the seam for the duration of one test.

    Returns:
        The exporter that will receive every span the configured provider
        finishes, readable via `get_finished_spans()`.
    """
    exporter = InMemorySpanExporter()

    def in_memory_only() -> tuple[SpanProcessor, ...]:
        return (SimpleSpanProcessor(exporter),)

    monkeypatch.setattr(tracing, "_build_span_processors", in_memory_only)

    return exporter


@pytest.fixture
def captured_spans(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Run the real tracing configuration with its spans captured in memory.

    Args:
        monkeypatch: Passed through to :func:`install_in_memory_exporter`.

    Returns:
        The exporter holding every span finished after configuration.
    """
    exporter = install_in_memory_exporter(monkeypatch)

    configure_tracing()

    return exporter


@pytest.fixture
def stubbed_database() -> Iterator[None]:
    """Serve the health route from a fake session, so no engine is ever built.

    Yields:
        Control to the test, with `get_db_session` overridden by a session whose
        `execute()` resolves to an opaque result. The payload is irrelevant —
        `HealthService` only cares that the statement did not raise — and a real
        result object would couple this file to SQLAlchemy internals for nothing.
    """
    session = AsyncMock(spec=AsyncSession)
    session.execute.return_value = MagicMock(name="Result")

    async def fake_db_session() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, session)

    app.dependency_overrides[get_db_session] = fake_db_session

    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db_session, None)


def server_spans(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    """Select the request spans from everything the instrumentation produced.

    Args:
        exporter: The in-memory exporter holding the finished spans.

    Returns:
        The server-kind spans, in completion order.
    """
    return [span for span in exporter.get_finished_spans() if span.kind is SpanKind.SERVER]


def test_configure_tracing_registers_a_real_sdk_tracer_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Tracing must be backed by the SDK, not by the API's silent no-op default.

    """
    install_in_memory_exporter(monkeypatch)

    assert not isinstance(trace.get_tracer_provider(), SDKTracerProvider)

    configure_tracing()

    provider = trace.get_tracer_provider()

    assert isinstance(provider, SDKTracerProvider)
    assert provider.resource.attributes[ResourceAttributes.SERVICE_NAME] == SERVICE_NAME
    assert provider.resource.attributes[ResourceAttributes.SERVICE_VERSION] == version(
        DISTRIBUTION_NAME
    )


@pytest.mark.usefixtures("stubbed_database")
def test_healthz_request_produces_one_server_span(captured_spans: InMemorySpanExporter) -> None:
    """
    Every HTTP request must be traced automatically, with route attributes.

    """
    response = TestClient(app).get(HEALTHZ_URL)

    assert response.status_code == 200

    requests = server_spans(captured_spans)

    assert len(requests) == 1

    span = requests[0]
    attributes = span.attributes or {}

    assert span.name == HEALTHZ_SPAN_NAME
    assert attributes[HTTP_METHOD_ATTRIBUTE] == "GET"
    assert attributes[HTTP_ROUTE_ATTRIBUTE] == HEALTHZ_URL
    assert attributes[HTTP_STATUS_CODE_ATTRIBUTE] == 200


def test_span_processors_pair_console_with_simple_and_otlp_with_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    `_build_span_processors` must be exercised for real at least once.

    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid:4317")
    get_settings.cache_clear()

    try:
        monkeypatch.setenv("OTEL_CONSOLE_EXPORT_ENABLED", "true")
        get_settings.cache_clear()
        processors = tracing._build_span_processors()

        assert len(processors) == 2
        console, otlp = processors
        assert isinstance(console, SimpleSpanProcessor)
        assert isinstance(console.span_exporter, ConsoleSpanExporter)
        assert isinstance(otlp, BatchSpanProcessor)
        assert isinstance(otlp.span_exporter, OTLPSpanExporter)

        monkeypatch.setenv("OTEL_CONSOLE_EXPORT_ENABLED", "false")
        get_settings.cache_clear()
        processors = tracing._build_span_processors()

        assert len(processors) == 1
        (otlp,) = processors
        assert isinstance(otlp, BatchSpanProcessor)
        assert isinstance(otlp.span_exporter, OTLPSpanExporter)
    finally:
        get_settings.cache_clear()


def test_get_tracer_returns_a_tracer_that_records_manual_spans(
    captured_spans: InMemorySpanExporter,
) -> None:
    """
    A hand-written span must reach the configured pipeline, under its own scope.

    """
    with get_tracer(__name__).start_as_current_span(MANUAL_SPAN_NAME):
        pass

    spans = captured_spans.get_finished_spans()

    assert [span.name for span in spans] == [MANUAL_SPAN_NAME]

    scope = spans[0].instrumentation_scope

    assert scope is not None
    assert scope.name == __name__
