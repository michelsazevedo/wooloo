"""
Process-wide OpenTelemetry tracing configuration.

"""

from importlib.metadata import version
from typing import Final

from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.semconv.resource import ResourceAttributes
from opentelemetry.trace import set_tracer_provider

from wooloo.config.settings import get_settings
from wooloo.infrastructure.logging.logger import logger

_SERVICE_NAME = "wooloo"
"""
Value of the ``service.name`` resource attribute, by which traces are grouped in any
backend.

"""

_DISTRIBUTION_NAME = "wooloo"
"""
Installed distribution whose metadata supplies ``service.version``.

Coincides with :data:`_SERVICE_NAME` today, but they answer different questions —
which service emitted the span, versus where its version number is read from — and
only one of them is the name a packaging change could move.

"""

_OTLP_EXPORT_TIMEOUT_SECONDS: Final = 2
"""
Ceiling on a single OTLP export attempt, and the only timeout in this system that
actually bounds shutdown.

"""


def _build_span_processors() -> tuple[SpanProcessor, ...]:
    """Return the span processors every span is fed through.

    Returns:
        A :class:`~opentelemetry.sdk.trace.export.BatchSpanProcessor` over the OTLP
        gRPC exporter, preceded by a
        :class:`~opentelemetry.sdk.trace.export.SimpleSpanProcessor` over the
        console exporter when console export is enabled.
    """
    settings = get_settings()

    processors: tuple[SpanProcessor, ...] = ()

    if settings.otel_console_export_enabled:
        processors += (SimpleSpanProcessor(ConsoleSpanExporter()),)

    return processors + (
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=settings.otel_exporter_otlp_endpoint,
                timeout=_OTLP_EXPORT_TIMEOUT_SECONDS,
            )
        ),
    )


def configure_tracing() -> None:
    """Register the process-wide tracer provider.

    Raises:
        importlib.metadata.PackageNotFoundError: If ``wooloo`` is not installed in
            the running environment, leaving no metadata to read the version from.
        pydantic.ValidationError: If settings cannot be loaded, via
            :func:`~wooloo.config.settings.get_settings`.
    """
    settings = get_settings()

    resource = Resource.create(
        {
            ResourceAttributes.SERVICE_NAME: _SERVICE_NAME,
            ResourceAttributes.SERVICE_VERSION: version(_DISTRIBUTION_NAME),
        }
    )

    provider = TracerProvider(resource=resource)
    for processor in _build_span_processors():
        provider.add_span_processor(processor)

    set_tracer_provider(provider)

    logger.info(
        "tracing_configured",
        endpoint=settings.otel_exporter_otlp_endpoint,
        console_export_enabled=settings.otel_console_export_enabled,
        export_timeout_seconds=_OTLP_EXPORT_TIMEOUT_SECONDS,
    )
