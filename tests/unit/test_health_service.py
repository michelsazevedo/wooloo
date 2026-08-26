"""
Unit tests for `HealthService`.

"""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from wooloo.application.services.health_service import (
    _PROBE_TIMEOUT_SECONDS,
    HealthService,
)
from wooloo.domain.storage.models import StoredBlob

DATABASE_FAILURES: list[BaseException] = [
    OperationalError("SELECT 1", None, Exception("connection refused")),
    TimeoutError("pool checkout timed out"),
    Exception("unexpected driver failure"),
]

PROBE_OVERRUN_FACTOR = 3.0

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
    """
    Stand in for a query the database accepts and then never answers.

    """
    await asyncio.sleep(_PROBE_TIMEOUT_SECONDS * PROBE_OVERRUN_FACTOR)


class FakeBlobStorage:
    """A `BlobStorage` whose readiness verdict is whatever the test configured.

    Attributes:
        check_health_calls: How many times the readiness probe was awaited, so a
            test can pin that the service asks once rather than once per field.
    """

    def __init__(self, *, healthy: bool = True, raises: BaseException | None = None) -> None:
        """Configure the double's answer.

        Args:
            healthy: What `check_health()` reports.
            raises: Raised by `check_health()` instead of returning, modelling an
                adapter bug — the real contract forbids it, which is exactly why
                the service's handling of it is worth pinning.
        """
        self._healthy = healthy
        self._raises = raises
        self.check_health_calls = 0

    async def put(
        self,
        content: AsyncIterator[bytes],
        *,
        content_type: str | None = None,
    ) -> StoredBlob:
        """Fail: a health probe must never write a blob."""
        raise AssertionError("put() is not part of the health probe")

    async def get(self, key: str) -> AsyncIterator[bytes]:
        """Fail: a health probe must never read a blob."""
        raise AssertionError("get() is not part of the health probe")

    async def exists(self, key: str) -> bool:
        """Fail: a health probe must never look a blob up."""
        raise AssertionError("exists() is not part of the health probe")

    async def size(self, key: str) -> int:
        """Fail: a health probe must never measure a blob."""
        raise AssertionError("size() is not part of the health probe")

    async def delete(self, key: str) -> None:
        """Fail: a health probe must never remove a blob."""
        raise AssertionError("delete() is not part of the health probe")

    async def check_health(self) -> bool:
        """Record the call, then raise or report as configured.

        Returns:
            The configured verdict.

        Raises:
            BaseException: The configured `raises`, if any.
        """
        self.check_health_calls += 1

        if self._raises is not None:
            raise self._raises

        return self._healthy


class StalledBlobStorage(FakeBlobStorage):
    """
    A backend whose readiness probe neither raises nor returns.

    Models the failure the `check_health()` contract does not cover: a wedged
    network mount, where the `stat`/`open` calls inside a filesystem probe block
    indefinitely rather than failing.

    """

    async def check_health(self) -> bool:
        """Block past the probe deadline.

        Returns:
            Never — the sleep outlives the caller's timeout.
        """
        self.check_health_calls += 1

        await asyncio.sleep(_PROBE_TIMEOUT_SECONDS * PROBE_OVERRUN_FACTOR)

        return True


def make_service(session: AsyncMock, storage: FakeBlobStorage | None = None) -> HealthService:
    """Inject the doubles, narrowing the session's type for the constructor.

    Args:
        session: The mocked session driving the database probe.
        storage: The double driving the storage probe. Defaults to a ready
            backend, so a test about the database says nothing about storage.

    Returns:
        A service bound to both doubles. Passing the fake in here is also what
        makes its conformance to the `BlobStorage` protocol a mypy-checked fact.
    """
    return HealthService(
        cast(AsyncSession, session),
        storage if storage is not None else FakeBlobStorage(),
    )


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
    """
    A silent database must degrade the response, not hold the request open.

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


@pytest.mark.parametrize("healthy", [True, False], ids=["ready", "unready"])
async def test_check_storage_reports_the_backends_own_verdict(healthy: bool) -> None:
    """The backend's answer is passed through, not re-derived here.

    Args:
        healthy: What the backend reports, and therefore what the service must.
    """
    storage = FakeBlobStorage(healthy=healthy)

    result = await make_service(make_session(), storage).check_storage()

    assert result is healthy
    assert storage.check_health_calls == 1


async def test_check_storage_propagates_an_adapter_bug() -> None:
    """
    A raising `check_health()` is a bug, and must not be dressed up as "down".

    """
    storage = FakeBlobStorage(raises=RuntimeError("adapter bug"))

    with pytest.raises(RuntimeError):
        await make_service(make_session(), storage).check_storage()


async def test_check_storage_abandons_a_probe_that_overruns_its_deadline() -> None:
    """
    A wedged storage volume must degrade the response, not hold the request open.

    """
    overrun_seconds = _PROBE_TIMEOUT_SECONDS * PROBE_OVERRUN_FACTOR
    storage = StalledBlobStorage()

    started = time.monotonic()
    result = await make_service(make_session(), storage).check_storage()
    elapsed = time.monotonic() - started

    assert result is False
    assert elapsed < overrun_seconds / 2
    assert storage.check_health_calls == 1


async def test_get_status_reports_ok_when_both_dependencies_are_reachable() -> None:
    """The healthy payload is the exact contract consumed by the API layer."""
    session = make_session()

    status = await make_service(session).get_status()

    assert status == {"status": "ok", "database": "up", "storage": "up"}


@pytest.mark.parametrize("failure", DATABASE_FAILURES, ids=lambda exc: type(exc).__name__)
async def test_get_status_reports_degraded_when_database_is_unreachable(
    failure: BaseException,
) -> None:
    """A failed probe degrades the payload without raising to the caller.

    Args:
        failure: The driver failure the probe hits.
    """
    session = make_session(failure)

    status = await make_service(session).get_status()

    assert status == {"status": "degraded", "database": "down", "storage": "up"}


async def test_get_status_reports_degraded_when_storage_is_unreachable() -> None:
    """
    An unreachable backend degrades the payload without touching `database`.

    """
    status = await make_service(make_session(), FakeBlobStorage(healthy=False)).get_status()

    assert status == {"status": "degraded", "database": "up", "storage": "down"}


async def test_get_status_does_not_degrade_further_when_both_are_down() -> None:
    """`status` is a two-value summary, not a severity scale.

    Two simultaneous failures degrade it exactly as far as one does; the
    per-dependency fields are what carry how much is actually broken.
    """
    session = make_session(DATABASE_FAILURES[0])

    status = await make_service(session, FakeBlobStorage(healthy=False)).get_status()

    assert status == {"status": "degraded", "database": "down", "storage": "down"}


async def test_get_status_returns_an_isolated_payload_per_call() -> None:
    """
    Callers must not be able to poison the module-level status constants.

    """
    service = make_service(make_session())

    first = await service.get_status()
    first["status"] = "mutated"
    second = await service.get_status()

    assert second == {"status": "ok", "database": "up", "storage": "up"}
