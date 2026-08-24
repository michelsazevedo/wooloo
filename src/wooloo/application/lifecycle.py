"""
Application startup and shutdown sequencing.

"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from sqlalchemy import text

from wooloo.config.settings import get_settings
from wooloo.infrastructure.database.engine import (
    dispose_engine,
    get_engine,
    get_session_factory,
)
from wooloo.infrastructure.logging.config import configure_logging
from wooloo.infrastructure.logging.logger import logger
from wooloo.infrastructure.telemetry.tracing import configure_tracing

_CONNECTIVITY_PROBE: Final = text("SELECT 1")

async def _verify_database_connectivity() -> None:
    """
    Probe the database once and record the outcome, without ever raising.

    """
    try:
        async with get_session_factory()() as session:
            await session.execute(_CONNECTIVITY_PROBE)
    except Exception as exc:
        logger.warning(
            "database_unreachable_at_startup",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return

    logger.info("database_connectivity_verified")


def _flush_tracer_provider() -> None:
    """
    Shut the global tracer provider down, exporting anything still buffered.

    """
    provider = trace.get_tracer_provider()

    if isinstance(provider, SDKTracerProvider):
        provider.shutdown()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Validate configuration at boot and release resources at shutdown.

    Args:
        _app: The application being started. Unused — state is held by the cached
            providers in their own modules rather than on ``app.state``.

    Yields:
        Control to the running application, once configuration is known good.

    Raises:
        pydantic.ValidationError: If ``DATABASE_URL`` is absent from both the
            environment and the ``.env`` file, or ``LOG_LEVEL`` is not a
            recognised level name.
        sqlalchemy.exc.ArgumentError: If ``DATABASE_URL`` is present but not a
            parseable SQLAlchemy DSN.
        importlib.metadata.PackageNotFoundError: If ``wooloo`` is not installed in
            the running environment, leaving no metadata to read the version from.
    """
    configure_logging()
    get_settings()
    get_engine()
    await _verify_database_connectivity()
    configure_tracing()
    
    logger.info("application_started")
    try:
        yield
    finally:
        await dispose_engine()
        _flush_tracer_provider()
        logger.info("application_stopped")
