"""
Unit tests for the application lifespan hook.

"""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager

import pytest
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from sqlalchemy.exc import ArgumentError
from sqlalchemy.pool import QueuePool
from structlog.typing import EventDict

from wooloo.application import lifecycle
from wooloo.config.settings import get_settings
from wooloo.infrastructure.database.engine import get_engine, get_session_factory
from wooloo.main import app, lifespan

pytestmark = pytest.mark.usefixtures("pristine_logging_state", "pristine_tracer_provider")

LIFESPAN_ENTRY_POINTS: list[Callable[[FastAPI], AbstractAsyncContextManager[None]]] = [
    lifespan,
    app.router.lifespan_context,
]

UNPARSEABLE_DATABASE_URL = "not-a-real-dsn"

PARSEABLE_DATABASE_URL = "postgresql+asyncpg://user:password@db.invalid:5432/wooloo"

LOCAL_POSTGRES_DATABASE_URL = "postgresql+asyncpg://wooloo:wooloopass@localhost:5432/wooloo"

UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://wooloo:wooloopass@127.0.0.1:1/wooloo"

STARTED_EVENT = "application_started"
STOPPED_EVENT = "application_stopped"
DATABASE_VERIFIED_EVENT = "database_connectivity_verified"
DATABASE_UNREACHABLE_EVENT = "database_unreachable_at_startup"

TRACING_CONFIGURED_EVENT = "tracing_configured"

EXPECTED_STARTUP_SEQUENCE = [
    "configure_logging",
    "get_settings",
    "get_engine",
    "_verify_database_connectivity",
    "configure_tracing",
    "log:application_started",
]

EXPECTED_SHUTDOWN_SEQUENCE = [
    "dispose_engine",
    "_flush_tracer_provider",
    "log:application_stopped",
]


@pytest.fixture(autouse=True)
def clear_provider_caches() -> Iterator[None]:
    """Isolate every test here from the process-wide provider caches."""
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def events_named(events: list[EventDict], name: str) -> list[EventDict]:
    """Select captured events by event name.

    Args:
        events: Everything captured during a test.
        name: The structlog event name to match.

    Returns:
        The matching events, in emission order.
    """
    return [event for event in events if event["event"] == name]


def event_names(events: list[EventDict]) -> list[str]:
    """Reduce captured events to their names, in emission order.

    Args:
        events: Everything captured during a test.

    Returns:
        The event names, suitable for asserting an exact log transcript.
    """
    return [str(event["event"]) for event in events]


def silence_configure_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the real logging bootstrap from dismantling the test's log capture.

    Args:
        monkeypatch: Used to substitute the name `lifecycle` resolves at call time.
    """

    def no_op() -> None:
        return None

    monkeypatch.setattr(lifecycle, "configure_logging", no_op)


def skip_database_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Substitute the connectivity probe for tests whose subject is not the database.

    Reaches for a private name on purpose. The alternative seams are worse: a DSN
    chosen to fail makes every such test also a test of a driver error path, and
    substituting `get_session_factory` builds a double whose only job is to be
    entered and ignored. The probe is the unit of behaviour being skipped, so it is
    the honest thing to name.

    Args:
        monkeypatch: Used to substitute the name `lifecycle` resolves at call time.
    """

    async def no_op() -> None:
        return None

    monkeypatch.setattr(lifecycle, "_verify_database_connectivity", no_op)


def skip_configure_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a test from registering a real tracer provider it has no use for.

    `pristine_tracer_provider` would contain the leak, but each real registration
    still spawns exporter threads and a gRPC channel; the tests that are not about
    tracing have nothing to gain from paying for that.

    Args:
        monkeypatch: Used to substitute the name `lifecycle` resolves at call time.
    """

    def no_op() -> None:
        return None

    monkeypatch.setattr(lifecycle, "configure_tracing", no_op)


def record_call(calls: list[str], name: str) -> Callable[..., None]:
    """Build a stand-in that notes it was called and does nothing else.

    Variadic so that it stays a valid substitute for a collaborator that grows an
    argument, rather than turning such a change into a `TypeError` in a test whose
    subject is ordering. Every startup collaborator takes nothing today.

    The recorder ignores arguments rather than asserting on them: this file pins
    *what runs when*, and what the collaborators themselves do is pinned in their
    own modules' test files — the ambient version's provenance, for instance, by
    `test_the_ambient_version_is_read_from_installed_package_metadata` in
    `test_logging_config.py`.

    Args:
        calls: The shared list every recorder appends to.
        name: The name to append when this recorder runs.

    Returns:
        A callable suitable for substituting a synchronous startup collaborator.
    """

    def recorder(*_args: object, **_kwargs: object) -> None:
        calls.append(name)

    return recorder


def record_async_call(calls: list[str], name: str) -> Callable[..., Awaitable[None]]:
    """Build the awaitable counterpart of `record_call`.

    Args:
        calls: The shared list every recorder appends to.
        name: The name to append when this recorder runs.

    Returns:
        A coroutine function suitable for substituting an awaited collaborator.
    """

    async def recorder(*_args: object, **_kwargs: object) -> None:
        calls.append(name)

    return recorder


class LoggerRecorder:
    """Stand-in for the module logger that notes event names in call order.

    Attributes:
        calls: The shared ordering list, appended to as ``log:<event>``.
    """

    def __init__(self, calls: list[str]) -> None:
        """Bind the recorder to the shared ordering list.

        Args:
            calls: The list this recorder appends to.
        """
        self.calls = calls

    def info(self, event: str, **_kwargs: object) -> None:
        """Record an info-level emission.

        Args:
            event: The structlog event name.
            **_kwargs: The record's fields. Unused — this recorder pins ordering,
                and the fields are asserted by the tests that read `captured_logs`.
        """
        self.calls.append(f"log:{event}")

    def warning(self, event: str, **_kwargs: object) -> None:
        """Record a warning-level emission.

        Args:
            event: The structlog event name.
            **_kwargs: The record's fields. Unused, as above.
        """
        self.calls.append(f"log:{event}")


@pytest.mark.parametrize("enter_lifespan", LIFESPAN_ENTRY_POINTS, ids=["hook", "wired-into-app"])
async def test_startup_fails_fast_when_database_url_is_unparseable(
    enter_lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Startup must abort on a malformed DSN instead of deferring it to a request.

    """
    monkeypatch.setenv("DATABASE_URL", UNPARSEABLE_DATABASE_URL)

    with pytest.raises(ArgumentError):
        async with enter_lifespan(FastAPI()):
            pytest.fail("startup yielded control despite an unparseable DATABASE_URL")


async def test_logs_application_started_when_startup_completes(
    monkeypatch: pytest.MonkeyPatch, captured_logs: list[EventDict]
) -> None:
    """
    A successful boot must announce itself, before the process takes traffic.

    """
    monkeypatch.setenv("DATABASE_URL", PARSEABLE_DATABASE_URL)
    silence_configure_logging(monkeypatch)
    skip_database_probe(monkeypatch)
    skip_configure_tracing(monkeypatch)

    async with lifespan(FastAPI()):
        started = events_named(captured_logs, STARTED_EVENT)

        assert len(started) == 1
        assert started[0]["log_level"] == "info"


async def test_logs_application_stopped_when_shutdown_runs(
    monkeypatch: pytest.MonkeyPatch, captured_logs: list[EventDict]
) -> None:
    """
    Shutdown must leave a record, so a disappearance can be told from a crash.

    """
    monkeypatch.setenv("DATABASE_URL", PARSEABLE_DATABASE_URL)
    silence_configure_logging(monkeypatch)
    skip_database_probe(monkeypatch)
    skip_configure_tracing(monkeypatch)

    async with lifespan(FastAPI()):
        assert events_named(captured_logs, STOPPED_EVENT) == []

    stopped = events_named(captured_logs, STOPPED_EVENT)

    assert len(stopped) == 1
    assert stopped[0]["log_level"] == "info"
    assert event_names(captured_logs) == [STARTED_EVENT, STOPPED_EVENT]


async def test_configures_logging_before_the_fail_fast_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Logging must be configured before anything that can fail, or it is pointless.

    """
    calls: list[str] = []
    monkeypatch.setattr(lifecycle, "configure_logging", record_call(calls, "configure_logging"))
    monkeypatch.setattr(lifecycle, "get_settings", record_call(calls, "get_settings"))
    monkeypatch.setattr(lifecycle, "get_engine", record_call(calls, "get_engine"))
    monkeypatch.setattr(
        lifecycle,
        "_verify_database_connectivity",
        record_async_call(calls, "_verify_database_connectivity"),
    )
    monkeypatch.setattr(lifecycle, "configure_tracing", record_call(calls, "configure_tracing"))
    monkeypatch.setattr(lifecycle, "dispose_engine", record_async_call(calls, "dispose_engine"))
    monkeypatch.setattr(
        lifecycle, "_flush_tracer_provider", record_call(calls, "_flush_tracer_provider")
    )
    monkeypatch.setattr(lifecycle, "logger", LoggerRecorder(calls))

    async with lifespan(FastAPI()):
        # Inside the context: startup has finished and shutdown has not begun, so
        # this is the complete startup sequence and nothing else.
        assert calls == EXPECTED_STARTUP_SEQUENCE

    assert calls == EXPECTED_STARTUP_SEQUENCE + EXPECTED_SHUTDOWN_SEQUENCE


async def test_registers_the_tracer_provider_before_yielding_control(
    monkeypatch: pytest.MonkeyPatch, captured_logs: list[EventDict]
) -> None:
    """
    Tracing must be configured during startup, not lazily on first use.

    """
    monkeypatch.setenv("DATABASE_URL", PARSEABLE_DATABASE_URL)
    silence_configure_logging(monkeypatch)
    skip_database_probe(monkeypatch)

    calls: list[str] = []
    monkeypatch.setattr(lifecycle, "configure_tracing", record_call(calls, "configure_tracing"))

    assert calls == []

    async with lifespan(FastAPI()):
        assert calls == ["configure_tracing"]

    assert calls == ["configure_tracing"]
    assert event_names(captured_logs) == [STARTED_EVENT, STOPPED_EVENT]


async def test_logs_database_connectivity_verified_against_a_real_database(
    monkeypatch: pytest.MonkeyPatch, captured_logs: list[EventDict]
) -> None:
    """
    A reachable database must be confirmed by an actual round trip.

    """
    monkeypatch.setenv("DATABASE_URL", LOCAL_POSTGRES_DATABASE_URL)
    silence_configure_logging(monkeypatch)

    async with lifespan(FastAPI()):
        verified = events_named(captured_logs, DATABASE_VERIFIED_EVENT)

        assert len(verified) == 1
        assert verified[0]["log_level"] == "info"
        assert events_named(captured_logs, DATABASE_UNREACHABLE_EVENT) == []
        assert event_names(captured_logs) == [
            DATABASE_VERIFIED_EVENT,
            TRACING_CONFIGURED_EVENT,
            STARTED_EVENT,
        ]

        pool = get_engine().pool

        assert isinstance(pool, QueuePool)
        assert pool.checkedin() == 1

        assert isinstance(trace.get_tracer_provider(), SDKTracerProvider)

    assert event_names(captured_logs) == [
        DATABASE_VERIFIED_EVENT,
        TRACING_CONFIGURED_EVENT,
        STARTED_EVENT,
        STOPPED_EVENT,
    ]


async def test_unreachable_database_is_logged_without_failing_startup(
    monkeypatch: pytest.MonkeyPatch, captured_logs: list[EventDict]
) -> None:
    """
    An unreachable database must degrade the boot, never abort it.

    """
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DATABASE_URL)
    silence_configure_logging(monkeypatch)
    skip_configure_tracing(monkeypatch)

    async with lifespan(FastAPI()):
        unreachable = events_named(captured_logs, DATABASE_UNREACHABLE_EVENT)

        assert len(unreachable) == 1
        assert unreachable[0]["log_level"] == "warning"
        assert unreachable[0]["error_type"] == ConnectionRefusedError.__name__
        assert str(unreachable[0]["error"]) != ""
        assert events_named(captured_logs, DATABASE_VERIFIED_EVENT) == []
        assert event_names(captured_logs) == [DATABASE_UNREACHABLE_EVENT, STARTED_EVENT]

    assert event_names(captured_logs) == [DATABASE_UNREACHABLE_EVENT, STARTED_EVENT, STOPPED_EVENT]
