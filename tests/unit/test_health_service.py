"""Unit tests for `HealthService`.

These tests never open a database connection: the only session ever handed to
`HealthService` is an `AsyncMock(spec=AsyncSession)`, so there is no engine, no
pool, and no network. Spec'ing the mock keeps the double honest — if
`AsyncSession.execute` were renamed or changed shape upstream, these tests fail
loudly instead of passing against an interface that no longer exists.

The failure paths are driven through the mocked session rather than by patching
`check_database`, so `get_status` is exercised over its real collaborator seam.
"""

import asyncio
import time
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from wooloo.application.services.health_service import (
    _PROBE_TIMEOUT_SECONDS,
    HealthService,
)

# OperationalError is the representative production failure: SQLAlchemy wraps
# connection refused, pool exhaustion, and server-side termination in it. The
# plain Exception and TimeoutError cases prove the probe's catch is genuinely
# broad and not narrowed to SQLAlchemyError, which is what the service documents.
DATABASE_FAILURES: list[BaseException] = [
    OperationalError("SELECT 1", None, Exception("connection refused")),
    TimeoutError("pool checkout timed out"),
    Exception("unexpected driver failure"),
]

# How far the stalled probe below overruns the service's deadline. Expressed as a
# multiple of `_PROBE_TIMEOUT_SECONDS` — imported rather than restated — so the
# overrun test tracks the real deadline instead of a copy that can drift from it.
PROBE_OVERRUN_FACTOR = 3.0

# Ceiling the deadline must stay under to be worth having at all: a bound that
# outlasts an orchestrator's own probe `timeoutSeconds` is enforced by the
# orchestrator instead of by us. Asserted before the overrun test sleeps, so a
# deadline widened to minutes fails the suite in milliseconds rather than hanging it.
MAX_USEFUL_PROBE_TIMEOUT_SECONDS = 5.0


def make_session(failure: BaseException | None = None) -> AsyncMock:
    """Build an isolated mocked `AsyncSession` for a single test.

    Args:
        failure: When set, `execute()` raises it. Otherwise `execute()` resolves
            to an opaque result object — the service ignores the payload, so a
            realistic `Result` would only couple the test to SQLAlchemy internals.

    Returns:
        A fresh `AsyncMock`; no state is shared between tests.
    """
    session = AsyncMock(spec=AsyncSession)
    if failure is None:
        session.execute.return_value = MagicMock(name="Result")
    else:
        session.execute.side_effect = failure
    return session


async def stall_past_the_probe_deadline(*_args: object, **_kwargs: object) -> None:
    """Stand in for a query the database accepts and then never answers.

    Used as `execute`'s `side_effect`. Sleeping rather than raising is what
    separates this from the failure cases above: nothing here ever produces an
    error, so the only thing that can make `check_database` return is its own
    deadline expiring.
    """
    await asyncio.sleep(_PROBE_TIMEOUT_SECONDS * PROBE_OVERRUN_FACTOR)


def make_service(session: AsyncMock) -> HealthService:
    """Inject the mocked session, narrowing the type for the constructor."""
    return HealthService(cast(AsyncSession, session))


async def test_check_database_returns_true_when_probe_succeeds() -> None:
    """A successful probe reports the database as reachable."""
    session = make_session()

    result = await make_service(session).check_database()

    assert result is True
    session.execute.assert_awaited_once()


async def test_check_database_issues_a_single_readonly_probe() -> None:
    """The connectivity check must stay a cheap, read-only `SELECT 1`.

    Guards against the probe drifting into an expensive or write-bearing query,
    which would turn a health endpoint into a load source.
    """
    session = make_session()

    await make_service(session).check_database()

    session.execute.assert_awaited_once()
    statement = session.execute.await_args.args[0]
    assert str(statement) == "SELECT 1"


@pytest.mark.parametrize("failure", DATABASE_FAILURES, ids=lambda exc: type(exc).__name__)
async def test_check_database_returns_false_when_probe_fails(failure: BaseException) -> None:
    """Any probe failure is reported as unreachable rather than propagated."""
    session = make_session(failure)

    result = await make_service(session).check_database()

    assert result is False


async def test_check_database_abandons_a_probe_that_overruns_its_deadline() -> None:
    """A silent database must degrade the response, not hold the request open.

    The engine's driver-level timeouts cover the failures asyncpg can observe;
    this deadline covers the one it cannot — a host that completed its handshake
    and then went quiet, leaving the query waiting on the OS TCP timeout. Since
    the pool caps concurrency at `_POOL_SIZE + _MAX_OVERFLOW`, enough concurrent
    probes in that state pin every connection and starve real traffic, which is
    what makes an unbounded probe on an unauthenticated endpoint a
    resource-exhaustion vector rather than merely a slow response.

    Asserting on elapsed time is what makes this a timeout test rather than a
    "returns False eventually" test: it fails if the `asyncio.timeout` wrapper is
    dropped, because the probe would then run to completion and report success.
    """
    assert 0 < _PROBE_TIMEOUT_SECONDS <= MAX_USEFUL_PROBE_TIMEOUT_SECONDS
    overrun_seconds = _PROBE_TIMEOUT_SECONDS * PROBE_OVERRUN_FACTOR
    session = make_session()
    session.execute.side_effect = stall_past_the_probe_deadline

    started = time.monotonic()
    result = await make_service(session).check_database()
    elapsed = time.monotonic() - started

    assert result is False
    assert elapsed < overrun_seconds / 2


async def test_check_database_propagates_cancellation() -> None:
    """Request cancellation must not be misreported as "database down".

    `CancelledError` derives from `BaseException`, so the service's broad
    `except Exception` lets it through. Widening that catch to `BaseException`
    would silently swallow cancellation and hang shutdown — this test pins it.
    """
    session = make_session(asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await make_service(session).check_database()


async def test_get_status_reports_ok_when_database_is_reachable() -> None:
    """The healthy payload is the exact contract consumed by the API layer."""
    session = make_session()

    status = await make_service(session).get_status()

    assert status == {"status": "ok", "database": "up"}


@pytest.mark.parametrize("failure", DATABASE_FAILURES, ids=lambda exc: type(exc).__name__)
async def test_get_status_reports_degraded_when_database_is_unreachable(
    failure: BaseException,
) -> None:
    """A failed probe degrades the payload without raising to the caller."""
    session = make_session(failure)

    status = await make_service(session).get_status()

    assert status == {"status": "degraded", "database": "down"}


async def test_get_status_returns_an_isolated_payload_per_call() -> None:
    """Callers must not be able to poison the module-level status constants.

    `get_status` copies its constants; dropping that copy would let a single
    mutating caller corrupt every subsequent health response process-wide.
    """
    service = make_service(make_session())

    first = await service.get_status()
    first["status"] = "mutated"
    second = await service.get_status()

    assert second == {"status": "ok", "database": "up"}
