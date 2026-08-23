"""Unit tests for the application lifespan hook.

`main.lifespan` exists for one reason: to turn a misconfigured deployment into a
process that refuses to start, rather than one that boots, reports itself healthy
to an orchestrator, and serves HTTP 500s. That guarantee is invisible in the code
— it is three statements with no assertions — so nothing but a test distinguishes
the real hook from a bare ``yield``.

Kept apart from `test_engine.py` deliberately: that file tests the infrastructure
module in isolation, while this one tests the composition root's use of it, which
is a different seam and a different reason to change.

Two entry points
----------------
The guarantee can be lost in two independent ways: by hollowing out the hook, and
by leaving the hook intact but never wiring it into the application. The test is
parametrized over both — the function itself, and the context the built `app`
actually runs at startup — because covering only the first would let
``FastAPI()`` silently drop the hook with a green suite.

The wired case reads `router.lifespan_context`, Starlette's own attribute, and
*enters* it. FastAPI wraps the hook in a merged closure, so asserting identity
against `lifespan` would couple this file to a private implementation detail and
still prove nothing about behaviour.

No real database
----------------
Nothing here opens a socket. The DSN under test is unparseable, so
`create_async_engine` raises while validating it and never reaches a driver. The
lifespan is driven directly as an async context manager rather than through
`TestClient`, which keeps the failure visible as the exception it is instead of a
transport-level error.

Cache hygiene
-------------
`get_settings`, `get_engine`, and `get_session_factory` are `lru_cache`'d, so they
are process-wide mutable state shared with every other test in the session. All
three are cleared on the way in and on the way out: without the clear on the way
in, settings built earlier would shadow this file's environment variable and the
lifespan would succeed; without the clear on the way out, this file's broken DSN
would leak into `tests/integration/test_health_endpoint.py`, which asserts on the
engine cache.
"""

from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager

import pytest
from fastapi import FastAPI
from sqlalchemy.exc import ArgumentError

from wooloo.config.settings import get_settings
from wooloo.infrastructure.database.engine import get_engine, get_session_factory
from wooloo.main import app, lifespan

# The hook as written, and the hook as the application will actually run it.
LIFESPAN_ENTRY_POINTS: list[Callable[[FastAPI], AbstractAsyncContextManager[None]]] = [
    lifespan,
    app.router.lifespan_context,
]

# Accepted by pydantic — `database_url` is typed `str`, so validation passes — and
# rejected by SQLAlchemy, which cannot find a dialect in it. Choosing a DSN that
# fails at exactly one known layer is what lets the assertion name a concrete
# exception type instead of a bare `Exception`, so the test proves the lifespan
# fails *for the configured reason* rather than merely failing.
UNPARSEABLE_DATABASE_URL = "not-a-real-dsn"


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


@pytest.mark.parametrize(
    "enter_lifespan", LIFESPAN_ENTRY_POINTS, ids=["hook", "wired-into-app"]
)
async def test_startup_fails_fast_when_database_url_is_unparseable(
    enter_lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup must abort on a malformed DSN instead of deferring it to a request.

    Settings and engine are both lazy and cached, so without this hook a malformed
    ``DATABASE_URL`` stays invisible until the first request touches the database
    and then surfaces as a raw 500 — after the process has already reported itself
    ready and started taking traffic. Raising here is what keeps a misconfigured
    deployment out of the load balancer.

    The exception type is asserted concretely: `ArgumentError` is what
    `create_async_engine` raises for an unparseable URL, so this fails if the
    startup work is reduced to a bare ``yield``, if the hook is never wired into
    the app, and also if startup starts failing for an unrelated reason.
    """
    monkeypatch.setenv("DATABASE_URL", UNPARSEABLE_DATABASE_URL)

    with pytest.raises(ArgumentError):
        async with enter_lifespan(FastAPI()):
            pytest.fail("startup yielded control despite an unparseable DATABASE_URL")
