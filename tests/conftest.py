"""
Fixtures shared by every test module.

"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

import pytest
import structlog
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from opentelemetry.trace import ProxyTracer, TracerProvider
from opentelemetry.util._once import Once
from structlog.typing import EventDict

from wooloo.main import app as _app

_MANAGED_LOGGERS: Final = ("", "wooloo", "uvicorn", "uvicorn.error", "uvicorn.access")
"""
Every logger :func:`wooloo.infrastructure.logging.config.configure_logging` touches.

The empty string is the root logger. ``uvicorn`` and ``uvicorn.error`` have their
handlers cleared and are forced to propagate; ``uvicorn.access`` is disabled
outright; ``wooloo`` and the root logger have their levels set.

"""


@dataclass(frozen=True)
class _LoggerState:
    """One stdlib logger's mutable configuration, captured for later restoration.

    Attributes:
        handlers: The logger's handlers, snapshotted as a tuple so that clearing
            the live list cannot empty the snapshot with it.
        level: The logger's own level, ``logging.NOTSET`` when it inherits.
        propagate: Whether records travel to ancestor loggers.
        disabled: Whether the logger drops everything.
    """

    handlers: tuple[logging.Handler, ...]
    level: int
    propagate: bool
    disabled: bool


def _snapshot_logger(name: str) -> _LoggerState:
    """Capture one logger's mutable configuration.

    Args:
        name: The logger name, ``""`` for the root logger.

    Returns:
        The state needed to put that logger back exactly as it was found.
    """
    target = logging.getLogger(name)

    return _LoggerState(
        handlers=tuple(target.handlers),
        level=target.level,
        propagate=target.propagate,
        disabled=target.disabled,
    )


def _restore_logger(name: str, state: _LoggerState) -> None:
    """Put one logger back into a previously captured state.

    Handlers are restored by clearing and extending the live list rather than by
    rebinding ``logger.handlers``: other code — including
    :func:`configure_logging` itself — holds and mutates that list object, so
    replacing it would leave those references pointing at a detached list.

    Args:
        name: The logger name, ``""`` for the root logger.
        state: The state captured by :func:`_snapshot_logger`.
    """
    target = logging.getLogger(name)

    target.handlers.clear()
    target.handlers.extend(state.handlers)
    target.setLevel(state.level)
    target.propagate = state.propagate
    target.disabled = state.disabled


@pytest.fixture(autouse=True)
def clean_structlog_contextvars() -> Iterator[None]:
    """
    Guarantee each test starts and ends with no bound contextvars.

    """
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


@pytest.fixture
def captured_logs() -> Iterator[list[EventDict]]:
    """Collect every structlog event emitted during the test, in order.

    Yields:
        A list that accumulates one event dict per log call as the test runs.
        Each dict holds the event name under ``"event"``, the level under
        ``"log_level"``, and every keyword passed to the logger, plus the merged
        contextvars.
    """
    with structlog.testing.capture_logs(
        processors=[structlog.contextvars.merge_contextvars]
    ) as events:
        yield events


@pytest.fixture
def pristine_logging_state() -> Iterator[None]:
    """Contain the blast radius of a test that runs the real ``configure_logging()``.

    Yields:
        Control to the test, with the current logging and structlog configuration
        recorded.
    """
    logger_states = {name: _snapshot_logger(name) for name in _MANAGED_LOGGERS}

    structlog_config = dict(structlog.get_config())
    structlog_config["processors"] = list(structlog_config["processors"])

    try:
        yield
    finally:
        for name, state in logger_states.items():
            _restore_logger(name, state)
        structlog.configure(**structlog_config)


def _force_tracer_provider(provider: TracerProvider | None) -> None:
    """Put OpenTelemetry's global tracer provider back to a chosen value.

    Args:
        provider: The provider to reinstate, or ``None`` if none had been set,
            which restores the API's lazily-returned no-op proxy.
    """
    trace._TRACER_PROVIDER = provider

    guard = Once()
    if provider is not None:
        guard.do_once(lambda: None)
    trace._TRACER_PROVIDER_SET_ONCE = guard


def _release_instrumentation_tracer() -> None:
    """
    Drop the provider :mod:`wooloo.main`'s FastAPI instrumentation has cached.

    """
    layer: object | None = _app.build_middleware_stack()

    while layer is not None:
        tracer = getattr(layer, "tracer", None)

        if isinstance(tracer, ProxyTracer):
            tracer._real_tracer = None

        layer = getattr(layer, "app", None)


@pytest.fixture
def pristine_tracer_provider() -> Iterator[None]:
    """Give each test an unregistered tracer provider, and leave it as it was found.

    Yields:
        Control to the test, with no tracer provider registered and the
        instrumentation ready to bind to whichever one the test registers.
    """
    previous = trace._TRACER_PROVIDER

    _force_tracer_provider(None)
    _release_instrumentation_tracer()

    try:
        yield
    finally:
        installed = trace._TRACER_PROVIDER
        _release_instrumentation_tracer()
        _force_tracer_provider(previous)

        if installed is not previous and isinstance(installed, SDKTracerProvider):
            installed.shutdown()
