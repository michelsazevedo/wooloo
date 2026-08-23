"""Integration tests for ``GET /api/v1/healthz``.

Scope: the wiring, not the logic. `HealthService`'s up/down decision is already
pinned by `tests/unit/test_health_service.py`; what is untested until here is the
chain the route actually runs through — router mounted at ``/api/v1`` →
`get_health_service` → `get_db_session` → `HealthService.get_status` → JSON body.
A refactor that breaks any link in that chain (a changed prefix, a provider that
stops injecting the session, a handler that starts branching) fails here.

Only `get_db_session` is overridden. Overriding `get_health_service` or patching
`HealthService` would be easier and would prove nothing: it would skip the two
seams this file exists to cover. Substituting at the outermost infrastructure
boundary keeps every application-layer link real.

No real database
----------------
The only session ever reachable from these tests is an `AsyncMock`, so no engine
is built, no pool is opened, and no socket is created. That is true by
construction rather than by convention, and
`test_healthz_never_builds_the_real_engine` asserts it rather than trusting it.
The suite therefore passes with PostgreSQL stopped, which is what makes it usable
as a pre-merge gate on machines and CI runners with no database.
"""

from collections.abc import AsyncIterator, Iterator
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from wooloo.infrastructure.database.engine import get_db_session, get_engine
from wooloo.main import app

# Asserted as a literal rather than reversed from the router, so a change to the
# version prefix or the route path fails the test instead of following it.
HEALTHZ_URL = "/api/v1/healthz"

STATUS_HEALTHY = {"status": "ok", "database": "up"}
STATUS_DEGRADED = {"status": "degraded", "database": "down"}

# The failure modes the endpoint must absorb identically. `OperationalError` is
# the realistic one — SQLAlchemy wraps connection refused, pool exhaustion, and
# server-side termination in it — while `TimeoutError` and a bare `Exception`
# prove the endpoint degrades on any failure rather than only on SQLAlchemy's own
# hierarchy. Without these, a non-SQLAlchemy failure would escape as a 500 and
# Kubernetes would read a crash where the contract promises a degraded body.
DATABASE_FAILURES: list[Exception] = [
    OperationalError("SELECT 1", None, Exception("connection refused")),
    TimeoutError("pool checkout timed out"),
    Exception("unexpected driver failure"),
]


@pytest.fixture(autouse=True)
def clean_dependency_overrides() -> Iterator[None]:
    """Guarantee each test starts and ends with an unmodified application.

    `app` is module-level and shared process-wide, so an override left behind
    would silently reconfigure every later test in the session — including files
    that never mention `get_db_session`. Clearing on both sides makes the leak
    impossible in either direction rather than merely unlikely.
    """
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def client() -> TestClient:
    """Return a client for the real application object.

    Deliberately not used as a context manager: entering `TestClient` runs the
    application's lifespan, and this suite's contract is that it needs no
    database. Startup wiring is out of scope for a route-level test, and binding
    to it would couple these tests to future connection-opening startup hooks.
    """
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

    Mirrors the real dependency's shape — an async generator that yields a
    session — so FastAPI resolves it through the same code path it uses in
    production. A plain coroutine would still work today but would stop matching
    the moment the real dependency's teardown semantics matter.

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
    """The overridden session must actually reach `HealthService`.

    Without this, both status assertions could pass against a route that never
    touched the database at all — a hardcoded body would look identical. Pinning
    the probe statement also guards the endpoint against drifting into an
    expensive or write-bearing query, which would turn a Kubernetes probe polled
    every few seconds into a load source.
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
    """An unreachable database degrades the body but never the status code.

    HTTP 200 here is the explicit contract (PRD FR-7), not an oversight: reaching
    the handler proves the process is alive, and the database verdict rides in the
    body. This assertion is the guard against someone "fixing" the endpoint into a
    503 and silently breaking every probe configured to parse the body.
    """
    override_db_session(make_session(failure))

    response = client.get(HEALTHZ_URL)

    assert response.status_code == 200
    assert response.json() == STATUS_DEGRADED


def test_healthz_never_builds_the_real_engine(client: TestClient) -> None:
    """Serving the endpoint under override must not touch real infrastructure.

    Compares the engine cache before and after rather than asserting it is empty,
    so the check stays correct if a future real-database suite runs earlier in the
    same session. A regression that bypasses the override — a service reaching for
    the session factory directly, say — would build the engine here and be caught
    even on a machine where PostgreSQL happens to be running.
    """
    override_db_session(make_session())
    engines_before = get_engine.cache_info().currsize

    response = client.get(HEALTHZ_URL)

    assert response.status_code == 200
    assert get_engine.cache_info().currsize == engines_before


def test_dependency_overrides_do_not_leak_out_of_this_module() -> None:
    """The shared app is left exactly as this module found it.

    Passes in isolation and in any ordering: it asserts an invariant that must
    hold at every point outside a test body, so it fails only if the fixture's
    teardown is ever weakened or removed.
    """
    assert get_db_session not in app.dependency_overrides
