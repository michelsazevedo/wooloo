"""
Integration tests for ``GET /api/v1/healthz``.

"""

from collections.abc import AsyncIterator
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from _health_doubles import override_blob_storage
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from wooloo.infrastructure.database.engine import get_db_session, get_engine
from wooloo.main import app

HEALTHZ_URL = "/api/v1/healthz"

STATUS_HEALTHY = {"status": "ok", "database": "up", "storage": "up"}
STATUS_DATABASE_DOWN = {"status": "degraded", "database": "down", "storage": "up"}
STATUS_STORAGE_DOWN = {"status": "degraded", "database": "up", "storage": "down"}

DATABASE_FAILURES: list[Exception] = [
    OperationalError("SELECT 1", None, Exception("connection refused")),
    TimeoutError("pool checkout timed out"),
    Exception("unexpected driver failure"),
]


@pytest.fixture
def client() -> TestClient:
    """Return a client for the real application object, with storage pinned up.

    Returns:
        A client for `app`, with `get_blob_storage` already overridden.
    """
    override_blob_storage(healthy=True)

    return TestClient(app)


def make_session(failure: BaseException | None = None) -> AsyncMock:
    """Build an isolated fake `AsyncSession` for a single test.

    Args:
        failure: When set, `execute()` raises it — the database-down case.
            Otherwise `execute()` resolves to an opaque result: the service
            ignores the payload, so returning a realistic `Result` would only
            couple the test to SQLAlchemy internals.

    Returns:
        A fresh `AsyncMock` spec'd against `AsyncSession`. The spec keeps the
        double honest — if `execute` were renamed upstream, these tests fail
        loudly instead of passing against an interface that no longer exists.
    """
    session = AsyncMock(spec=AsyncSession)
    if failure is None:
        session.execute.return_value = MagicMock(name="Result")
    else:
        session.execute.side_effect = failure
    return session


def override_db_session(session: AsyncMock) -> None:
    """Substitute the fake session at the infrastructure boundary.

    Args:
        session: The fake session every request should receive.
    """

    async def _fake_db_session() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, session)

    app.dependency_overrides[get_db_session] = _fake_db_session


def test_healthz_reports_ok_when_database_is_reachable(client: TestClient) -> None:
    """A reachable database yields the healthy payload with HTTP 200."""
    override_db_session(make_session())

    response = client.get(HEALTHZ_URL)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == STATUS_HEALTHY


def test_healthz_probes_the_database_through_the_injected_session(client: TestClient) -> None:
    """
    The overridden session must actually reach `HealthService`.

    """
    session = make_session()
    override_db_session(session)

    client.get(HEALTHZ_URL)

    session.execute.assert_awaited_once()
    assert str(session.execute.await_args.args[0]) == "SELECT 1"


@pytest.mark.parametrize("failure", DATABASE_FAILURES, ids=lambda exc: type(exc).__name__)
def test_healthz_reports_degraded_when_database_is_unreachable(
    client: TestClient, failure: Exception
) -> None:
    """
    An unreachable database degrades the body but never the status code.
    """
    override_db_session(make_session(failure))

    response = client.get(HEALTHZ_URL)

    assert response.status_code == 200
    assert response.json() == STATUS_DATABASE_DOWN


def test_healthz_reports_storage_down_without_masking_a_healthy_database(
    client: TestClient,
) -> None:
    """
    A storage outage must degrade `status` and nothing else.

    """
    override_db_session(make_session())
    override_blob_storage(healthy=False)

    response = client.get(HEALTHZ_URL)

    assert response.status_code == 200
    assert response.json() == STATUS_STORAGE_DOWN


def test_healthz_never_builds_the_real_engine(client: TestClient) -> None:
    """
    Serving the endpoint under override must not touch real infrastructure.

    """
    override_db_session(make_session())
    engines_before = get_engine.cache_info().currsize

    response = client.get(HEALTHZ_URL)

    assert response.status_code == 200
    assert get_engine.cache_info().currsize == engines_before
