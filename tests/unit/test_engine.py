"""
Unit tests for the database engine module.

"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from types import TracebackType
from typing import Self, cast
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from wooloo.infrastructure.database import engine as engine_module
from wooloo.infrastructure.database.engine import (
    get_db_session,
    get_engine,
    get_session_factory,
)

FAKE_DATABASE_URL = "postgresql+asyncpg://user:password@db.invalid:5432/wooloo"


@pytest.fixture(autouse=True)
def clear_provider_caches() -> Iterator[None]:
    """
    Isolate every test from the process-wide provider caches.

    """
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture
def configured_database_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    Make `DATABASE_URL` resolvable without depending on the repo's ``.env``.

    """
    monkeypatch.setenv("DATABASE_URL", FAKE_DATABASE_URL)
    engine_module.get_settings.cache_clear()
    yield
    engine_module.get_settings.cache_clear()


class FakeSession:
    """
    Stand-in for the `AsyncSession` an `async_sessionmaker` produces.

    """

    def __init__(self) -> None:
        self.closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed = True


def install_fake_session_factory(monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    """
    Swap the session factory for one yielding a single observable session.

    Args:
        monkeypatch: Fixture used to undo the patch after the test.

    Returns:
        The session `get_db_session` will yield, for asserting on afterwards.
    """
    session = FakeSession()
    monkeypatch.setattr(engine_module, "get_session_factory", lambda: (lambda: session))
    return session


async def test_get_db_session_yields_a_session_from_the_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dependency hands the caller the factory's session, unwrapped."""
    session = install_fake_session_factory(monkeypatch)

    generator = get_db_session()
    yielded = await anext(generator)

    assert yielded is cast(AsyncSession, session)
    await generator.aclose()


async def test_get_db_session_closes_the_session_on_the_success_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normally exhausted generator must return its session to the pool.

    This is the ordinary case behind every served request: without the close, each
    request would leak a connection and the pool would be exhausted after
    `_POOL_SIZE + _MAX_OVERFLOW` requests.
    """
    session = install_fake_session_factory(monkeypatch)

    generator = get_db_session()
    await anext(generator)
    assert session.closed is False

    with pytest.raises(StopAsyncIteration):
        await anext(generator)

    assert session.closed is True


async def test_get_db_session_closes_the_session_when_the_caller_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handler that raises must still give its connection back.

    The failure path is the one that matters: an endpoint erroring under load is
    exactly when connections are scarcest, so a leak here converts a handled error
    into pool exhaustion. The exception must also reach the caller — swallowing it
    would turn a 500 into a silent success.
    """
    session = install_fake_session_factory(monkeypatch)
    failure = RuntimeError("handler blew up")

    generator = get_db_session()
    await anext(generator)

    with pytest.raises(RuntimeError) as raised:
        await generator.athrow(failure)

    assert raised.value is failure
    assert session.closed is True


async def test_get_db_session_closes_the_session_when_the_caller_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation is not an exception the cleanup may skip.

    `CancelledError` derives from `BaseException`, so a cleanup guarded by
    ``except Exception`` would let a cancelled request leak its connection. Client
    disconnects make this a routine path, not an exotic one.
    """
    session = install_fake_session_factory(monkeypatch)

    generator = get_db_session()
    await anext(generator)

    with pytest.raises(asyncio.CancelledError):
        await generator.athrow(asyncio.CancelledError())

    assert session.closed is True


def test_get_engine_is_cached(configured_database_url: None) -> None:
    """Every caller must share one pool.

    A second engine would mean a second connection pool, silently doubling the
    process's connection footprint against PostgreSQL's `max_connections`.
    """
    first = get_engine()
    second = get_engine()

    assert first is second
    assert isinstance(first, AsyncEngine)


def test_get_engine_is_constructible_without_a_live_database(
    configured_database_url: None,
) -> None:
    """Building the engine validates the DSN but opens no connection.

    This is what lets `main.lifespan` fail fast on a malformed `DATABASE_URL`
    without refusing to boot when PostgreSQL is merely restarting — and what lets
    this suite run on a machine with no database at all.
    """
    built = get_engine()

    assert built.url.drivername == "postgresql+asyncpg"
    assert built.url.database == "wooloo"


def test_get_session_factory_is_cached(configured_database_url: None) -> None:
    """The factory is shared and bound to the shared engine."""
    first = get_session_factory()
    second = get_session_factory()

    assert first is second
    assert isinstance(first, async_sessionmaker)
    assert first.kw["bind"] is get_engine()


def test_get_session_factory_disables_expire_on_commit(
    configured_database_url: None,
) -> None:
    """Expiry on commit would trigger implicit lazy I/O outside an ``await``.

    With an async session, re-fetching an attribute expired by a commit raises
    rather than quietly emitting a query, so this setting is load-bearing rather
    than a preference.
    """
    assert get_session_factory().kw["expire_on_commit"] is False


async def test_dispose_engine_is_a_no_op_when_no_engine_was_built() -> None:
    """Shutdown must be safe for a process that never touched the database.

    Without the guard, disposing would build an engine purely in order to throw it
    away — and would raise on a process with no `DATABASE_URL` configured, turning
    a clean shutdown into a crash.
    """
    assert get_engine.cache_info().currsize == 0

    await engine_module.dispose_engine()

    assert get_engine.cache_info().currsize == 0


async def test_dispose_engine_disposes_and_clears_both_caches(
    configured_database_url: None,
) -> None:
    """
    Disposal must tear the pool down and not leave the engine cached behind it.

    """
    built = get_engine()
    get_session_factory()
    pool_before = built.sync_engine.pool

    await engine_module.dispose_engine()

    assert built.sync_engine.pool is not pool_before
    assert get_engine.cache_info().currsize == 0
    assert get_session_factory.cache_info().currsize == 0


def test_engine_is_configured_with_connection_timeouts(
    monkeypatch: pytest.MonkeyPatch, configured_database_url: None
) -> None:
    """
    The driver-level bounds that stop a stalled host from pinning the pool.

    """
    recorded: dict[str, object] = {}

    def spy_create_async_engine(url: str, **kwargs: object) -> MagicMock:
        recorded.update(kwargs)
        return MagicMock(name="AsyncEngine")

    monkeypatch.setattr(engine_module, "create_async_engine", spy_create_async_engine)

    get_engine()

    assert recorded["connect_args"] == {"timeout": 3.0, "command_timeout": 3.0}
    assert recorded["pool_timeout"] == 5.0


def test_engine_pool_is_bounded(configured_database_url: None) -> None:
    """
    Pool sizing and pre-ping must actually reach the pool, not just the call.

    """
    pool = get_engine().sync_engine.pool

    assert pool.size() == 5
    assert pool._max_overflow == 10
    assert pool._pre_ping is True


def test_session_dep_resolves_through_get_db_session() -> None:
    """
    The exported alias must point at the real dependency.

    """
    dependency = engine_module.SessionDep.__metadata__[0]

    assert dependency.dependency is get_db_session


def test_get_db_session_returns_an_async_iterator(monkeypatch: pytest.MonkeyPatch) -> None:
    """FastAPI relies on the generator shape to run teardown after the response."""
    install_fake_session_factory(monkeypatch)

    generator = get_db_session()

    assert isinstance(generator, AsyncIterator)
