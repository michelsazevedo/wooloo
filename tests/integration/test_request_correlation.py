"""
Integration tests for request correlation across layers.

"""

from collections.abc import AsyncIterator
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from _health_doubles import override_blob_storage
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.typing import EventDict

from wooloo.infrastructure.database.engine import get_db_session
from wooloo.infrastructure.storage.deps import get_blob_storage
from wooloo.main import app, asgi_app

HEALTHZ_URL = "/api/v1/healthz"

HANDLER_EVENT = "health_check_requested"
DEGRADED_EVENT = "database_unavailable"
STORAGE_DEGRADED_EVENT = "storage_unavailable"
SUMMARY_EVENT = "http_request"


@pytest.fixture
def client() -> TestClient:
    """Return a client for the object the application actually serves.

    Returns:
        A client for `asgi_app`, with `get_blob_storage` already overridden.
    """
    override_blob_storage(healthy=True)

    return TestClient(asgi_app)


def override_db_session(failure: BaseException | None = None) -> None:
    """
    Substitute a fake session at the outermost infrastructure boundary.

    Args:
        failure: When set, `execute()` raises it, driving the database-down
            branch of the handler. Otherwise the probe succeeds.
    """

    async def _fake_db_session() -> AsyncIterator[AsyncSession]:
        session = AsyncMock(spec=AsyncSession)
        if failure is None:
            session.execute.return_value = MagicMock(name="Result")
        else:
            session.execute.side_effect = failure
        yield cast(AsyncSession, session)

    app.dependency_overrides[get_db_session] = _fake_db_session


def events_named(events: list[EventDict], name: str) -> list[EventDict]:
    """Select captured events by event name.

    Args:
        events: Everything captured during a test.
        name: The structlog event name to match.

    Returns:
        The matching events, in emission order.
    """
    return [event for event in events if event["event"] == name]


def test_the_real_application_correlates_every_response(
    client: TestClient, captured_logs: list[EventDict]
) -> None:
    """
    The middleware must actually be wrapped around the shipped application.

    """
    override_db_session()

    response = client.get(HEALTHZ_URL)

    assert response.status_code == 200
    assert response.headers["x-request-id"]
    assert len(events_named(captured_logs, SUMMARY_EVENT)) == 1


@pytest.mark.parametrize("inbound", [None, "client-supplied-id"], ids=["generated", "supplied"])
def test_a_handler_log_carries_the_middleware_request_id(
    client: TestClient, captured_logs: list[EventDict], inbound: str | None
) -> None:
    """
    The correlation ID must reach code that knows nothing about the middleware.

    Args:
        inbound: The `X-Request-ID` to send, or `None` to send no header.
    """
    override_db_session()
    headers = {} if inbound is None else {"X-Request-ID": inbound}

    response = client.get(HEALTHZ_URL, headers=headers)

    issued = response.headers["x-request-id"]
    if inbound is not None:
        assert issued == inbound

    handler_events = events_named(captured_logs, HANDLER_EVENT)
    summary_events = events_named(captured_logs, SUMMARY_EVENT)

    assert len(handler_events) == 1
    assert len(summary_events) == 1
    assert handler_events[0]["request_id"] == issued
    assert summary_events[0]["request_id"] == issued


def test_a_degraded_probe_stays_correlated(
    client: TestClient, captured_logs: list[EventDict]
) -> None:
    """
    Correlation must hold on the branch an operator actually investigates.

    """
    override_db_session(OperationalError("SELECT 1", None, Exception("connection refused")))

    response = client.get(HEALTHZ_URL)

    issued = response.headers["x-request-id"]
    assert response.json() == {"status": "degraded", "database": "down", "storage": "up"}

    warnings = events_named(captured_logs, DEGRADED_EVENT)
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["request_id"] == issued
    assert events_named(captured_logs, SUMMARY_EVENT)[0]["status_code"] == 200
    assert events_named(captured_logs, STORAGE_DEGRADED_EVENT) == []


def test_a_degraded_storage_probe_warns_under_its_own_name(
    client: TestClient, captured_logs: list[EventDict]
) -> None:
    """Each dependency's outage must be greppable by name, and correlated.

    An operator alerting on `storage_unavailable` has to find it when storage is
    the thing that broke — and must not find `database_unavailable` alongside it,
    which would send them to a database that is answering perfectly well.
    """
    override_db_session()
    override_blob_storage(healthy=False)

    response = client.get(HEALTHZ_URL)

    issued = response.headers["x-request-id"]
    assert response.json() == {"status": "degraded", "database": "up", "storage": "down"}

    warnings = events_named(captured_logs, STORAGE_DEGRADED_EVENT)
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["request_id"] == issued
    assert events_named(captured_logs, DEGRADED_EVENT) == []
    assert [event["event"] for event in captured_logs] == [
        HANDLER_EVENT,
        STORAGE_DEGRADED_EVENT,
        SUMMARY_EVENT,
    ]


def test_request_ids_do_not_leak_between_requests(
    client: TestClient, captured_logs: list[EventDict]
) -> None:
    """
    A second request must be correlated to itself and to nothing else.

    """
    override_db_session()

    first = client.get(HEALTHZ_URL)
    second = client.get(HEALTHZ_URL)

    first_id = first.headers["x-request-id"]
    second_id = second.headers["x-request-id"]
    assert first_id != second_id

    assert [event["event"] for event in captured_logs] == [
        HANDLER_EVENT,
        SUMMARY_EVENT,
        HANDLER_EVENT,
        SUMMARY_EVENT,
    ]
    assert [event["request_id"] for event in captured_logs] == [
        first_id,
        first_id,
        second_id,
        second_id,
    ]


def test_a_supplied_request_id_does_not_survive_into_the_next_request(
    client: TestClient, captured_logs: list[EventDict]
) -> None:
    """
    An echoed ID is per-request state, not a sticky default.

    """
    override_db_session()

    client.get(HEALTHZ_URL, headers={"X-Request-ID": "sticky-id"})
    second = client.get(HEALTHZ_URL)

    second_id = second.headers["x-request-id"]
    assert second_id != "sticky-id"

    later_events = captured_logs[2:]
    assert [event["request_id"] for event in later_events] == [second_id, second_id]
    assert all(event["request_id"] != "sticky-id" for event in later_events)


def test_correlation_does_not_alter_the_response_body(
    client: TestClient, captured_logs: list[EventDict]
) -> None:
    """Observability must be invisible to the client beyond one header.

    The middleware rewrites the outbound header list on `http.response.start`; a
    bug there could truncate the body or corrupt `Content-Type` while leaving
    every log assertion in this file green.
    """
    override_db_session()

    response = client.get(HEALTHZ_URL)

    assert response.json() == {"status": "ok", "database": "up", "storage": "up"}
    assert response.headers["content-type"].startswith("application/json")


def test_dependency_overrides_do_not_leak_between_integration_tests() -> None:
    """
    The shared app is left exactly as every module in this directory found it.

    """
    assert get_db_session not in app.dependency_overrides
    assert get_blob_storage not in app.dependency_overrides
