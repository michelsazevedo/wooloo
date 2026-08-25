"""
Integration tests for the repository HTTP surface.

"""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from structlog.typing import EventDict

from wooloo.config.settings import get_settings
from wooloo.infrastructure.database.engine import get_db_session
from wooloo.main import app, asgi_app

REPOSITORIES_URL: Final = "/api/v1/repositories"

RESPONSE_FIELDS: Final = {"id", "name", "created_at"}
"""
Exactly what `RepositoryResponse` exposes.

Asserted as a set equality rather than field-by-field presence, because the point
is as much what is *absent* — `updated_at` and `deleted_at` are deliberately not
part of the public shape — as what is there.

"""

ERROR_FIELDS: Final = {"code", "message", "request_id"}
"""
Exactly what `ErrorResponse` exposes, for the same reason.

"""

SUPPLIED_REQUEST_ID: Final = "client-supplied-id"

FAILURE_EVENT: Final = "request_failed"

CREATED_EVENT: Final = "repository_created"
RETRIEVED_EVENT: Final = "repository_retrieved"
DELETED_EVENT: Final = "repository_deleted"

INVALID_NAMES: Final[tuple[str, ...]] = (
    "Library/Nginx",
    "my repo",
    "@@invalid",
    "acme//backend",
    "/acme",
    "acme/",
    "",
)
"""
Names the OCI grammar rejects, one per rejection reason.

The grammar itself is pinned by `tests/unit/test_repository_name.py`; these rows
exist to prove the rejection *travels* — that `InvalidRepositoryName` raised deep
in the domain reaches the client as a `400` rather than escaping as a `500`.

"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def session_factory() -> Iterator[async_sessionmaker[AsyncSession]]:
    """Build a non-pooling session factory against the configured database.

    Yields:
        A factory producing sessions whose connections are opened and closed
        within whichever event loop uses them.
    """
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)

    try:
        yield async_sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        asyncio.run(engine.dispose())


@pytest.fixture
def client(session_factory: async_sessionmaker[AsyncSession]) -> TestClient:
    """Return a client for the served application, wired to the test engine.

    Args:
        session_factory: Supplies the request-scoped session.

    Returns:
        A client driving `asgi_app`, middleware included.
    """

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = _test_db_session

    return TestClient(asgi_app)


@pytest.fixture
def prefix(session_factory: async_sessionmaker[AsyncSession]) -> Iterator[str]:
    """Give one test a private namespace in the shared table, and reclaim it.

    Args:
        session_factory: Supplies the session the cleanup statement runs on.

    Yields:
        A valid OCI name component, unique to this test.
    """
    value = f"itest{uuid4().hex[:12]}"

    try:
        yield value
    finally:
        asyncio.run(_purge(session_factory, value))


async def _purge(factory: async_sessionmaker[AsyncSession], prefix: str) -> None:
    """Hard-delete every repository created under one prefix.

    Args:
        factory: Supplies the session.
        prefix: The per-test namespace whose rows should be removed.
    """
    async with factory() as session:
        await session.execute(
            text("DELETE FROM repositories WHERE name LIKE :pattern"),
            {"pattern": f"{prefix}%"},
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def create_repository(client: TestClient, name: str) -> Response:
    """Register one repository over HTTP.

    Args:
        client: The client to issue the request through.
        name: The repository name to create.

    Returns:
        The raw response, so callers can assert on failures too.
    """
    return client.post(REPOSITORIES_URL, json={"name": name})


def create_repository_body(client: TestClient, name: str) -> dict[str, str]:
    """Register one repository and assert it worked, returning its body.

    Args:
        client: The client to issue the request through.
        name: The repository name to create.

    Returns:
        The created repository's response body.
    """
    response = create_repository(client, name)
    assert response.status_code == 201, response.text

    body: dict[str, str] = response.json()

    return body


def list_repositories(client: TestClient, query: str = "?limit=100") -> dict[str, object]:
    """Read one page of repositories.

    Args:
        client: The client to issue the request through.
        query: Query string to append, including the leading `?`.

    Returns:
        The page body.
    """
    response = client.get(f"{REPOSITORIES_URL}{query}")
    assert response.status_code == 200, response.text

    body: dict[str, object] = response.json()

    return body


def page_items(page: dict[str, object]) -> list[dict[str, str]]:
    """Extract a page body's items, in page order.

    Args:
        page: A `RepositoryListResponse` body.

    Returns:
        Every item on the page, newest first.
    """
    items = page["items"]
    assert isinstance(items, list)

    return items


def owned_ids(page: dict[str, object], prefix: str) -> list[str]:
    """Extract the ids of one test's own repositories from a page, in page order.

    Args:
        page: A `RepositoryListResponse` body.
        prefix: The test's private namespace.

    Returns:
        The `id` of each item named under `prefix`, newest first.
    """
    return [item["id"] for item in page_items(page) if item["name"].startswith(prefix)]


def total_of(page: dict[str, object]) -> int:
    """Read a page's reported total.

    Args:
        page: A `RepositoryListResponse` body.

    Returns:
        The count of non-deleted repositories the server reported.
    """
    total = page["total"]
    assert isinstance(total, int)

    return total


def events_named(events: list[EventDict], name: str) -> list[EventDict]:
    """Select captured events by event name.

    Args:
        events: Everything captured during a test.
        name: The structlog event name to match.

    Returns:
        The matching events, in emission order.
    """
    return [event for event in events if event["event"] == name]


def single_event(events: list[EventDict], name: str) -> EventDict:
    """Return the one event with the given name, asserting there is exactly one.

    Args:
        events: Everything captured during a test.
        name: The structlog event name to match.

    Returns:
        The single matching event.
    """
    matches = events_named(events, name)
    assert len(matches) == 1, f"expected exactly one {name!r} event, got {len(matches)}"

    return matches[0]


# ---------------------------------------------------------------------------
# POST /api/v1/repositories
# ---------------------------------------------------------------------------


def test_creating_a_repository_returns_201_with_the_persisted_repository(
    client: TestClient, prefix: str
) -> None:
    """
    A valid name must yield `201` and the database-generated identity of the row.

    """
    name = f"{prefix}/backend-api"

    response = create_repository(client, name)

    assert response.status_code == 201
    assert response.headers["content-type"].startswith("application/json")

    body = response.json()
    assert set(body) == RESPONSE_FIELDS
    assert body["name"] == name
    assert UUID(body["id"]).version == 4
    assert datetime.fromisoformat(body["created_at"]).tzinfo is not None


def test_creating_a_duplicate_name_returns_409_conflict(
    client: TestClient, prefix: str
) -> None:
    """
    A name already taken must be refused as a conflict, without repeating it.

    """
    name = f"{prefix}/backend-api"
    create_repository_body(client, name)

    response = create_repository(client, name)

    assert response.status_code == 409
    body = response.json()
    assert set(body) == ERROR_FIELDS
    assert body["code"] == "conflict"
    assert body["message"]
    assert name not in response.text
    assert prefix not in response.text


def test_a_conflict_with_a_soft_deleted_name_leaks_nothing_either(
    client: TestClient, prefix: str
) -> None:
    """
    A soft-deleted repository still holds its name, and the refusal must not say so.

    """
    name = f"{prefix}/wooloo"
    created = create_repository_body(client, name)
    assert client.delete(f"{REPOSITORIES_URL}/{created['id']}").status_code == 204

    response = create_repository(client, name)

    assert response.status_code == 409
    body = response.json()
    assert set(body) == ERROR_FIELDS
    assert body["code"] == "conflict"
    assert body["message"]
    assert name not in response.text
    assert prefix not in response.text


def test_a_conflict_does_not_create_a_second_row(client: TestClient, prefix: str) -> None:
    """
    The refused duplicate must leave exactly one repository under that name.

    """
    name = f"{prefix}/backend-api"
    created = create_repository_body(client, name)

    assert create_repository(client, name).status_code == 409

    assert owned_ids(list_repositories(client), prefix) == [created["id"]]


@pytest.mark.parametrize("name", INVALID_NAMES, ids=lambda name: name or "empty")
def test_creating_an_invalid_name_returns_400_validation_error(
    client: TestClient, name: str
) -> None:
    """A name the OCI grammar rejects must surface as a client error, not a bug.

    Args:
        name: The name to attempt.
    """
    response = create_repository(client, name)

    assert response.status_code == 400
    body = response.json()
    assert set(body) == ERROR_FIELDS
    assert body["code"] == "validation_error"


# ---------------------------------------------------------------------------
# GET /api/v1/repositories
# ---------------------------------------------------------------------------


def test_listing_returns_created_repositories_newest_first_with_page_metadata(
    client: TestClient, prefix: str
) -> None:
    """
    The page must carry this test's rows, newest first, with the applied metadata.

    """
    older = create_repository_body(client, f"{prefix}/older")
    newer = create_repository_body(client, f"{prefix}/newer")

    page = list_repositories(client)

    assert owned_ids(page, prefix) == [newer["id"], older["id"]]
    assert page["limit"] == 100
    assert page["offset"] == 0
    assert total_of(page) >= len(page_items(page))


def test_the_total_counts_the_collection_rather_than_the_returned_page(
    client: TestClient, prefix: str
) -> None:
    """
    `total` must describe the whole collection, not the page it accompanies.

    """
    create_repository_body(client, f"{prefix}/older")
    create_repository_body(client, f"{prefix}/newer")

    page = list_repositories(client, "?limit=1")

    assert page["limit"] == 1
    assert len(page_items(page)) == 1
    assert total_of(page) >= 2


def test_listing_excludes_a_soft_deleted_repository(client: TestClient, prefix: str) -> None:
    """
    A deleted repository must vanish from the collection, its neighbour must not.

    """
    kept = create_repository_body(client, f"{prefix}/kept")
    wooloo = create_repository_body(client, f"{prefix}/wooloo")

    assert owned_ids(list_repositories(client), prefix) == [wooloo["id"], kept["id"]]

    assert client.delete(f"{REPOSITORIES_URL}/{wooloo['id']}").status_code == 204

    after = list_repositories(client)
    assert owned_ids(after, prefix) == [kept["id"]]
    assert total_of(after) >= len(page_items(after))


@pytest.mark.parametrize(
    ("query", "expected_limit", "expected_offset"),
    [
        ("?limit=500", 100, 0),
        ("?limit=0", 1, 0),
        ("?limit=-1", 1, 0),
        ("?offset=-5", 20, 0),
    ],
    ids=["limit-above-max", "limit-zero", "limit-negative", "offset-negative"],
)
def test_an_out_of_range_page_request_is_clamped_rather_than_rejected(
    client: TestClient, query: str, expected_limit: int, expected_offset: int
) -> None:
    """An unreasonable page request must be served, and must say what it served.

    Args:
        query: The query string to send.
        expected_limit: The `limit` the response must report.
        expected_offset: The `offset` the response must report.
    """
    response = client.get(f"{REPOSITORIES_URL}{query}")

    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == expected_limit
    assert body["offset"] == expected_offset
    assert len(body["items"]) <= expected_limit


# ---------------------------------------------------------------------------
# GET /api/v1/repositories/{repository_id}
# ---------------------------------------------------------------------------


def test_retrieving_a_repository_returns_exactly_what_creation_returned(
    client: TestClient, prefix: str
) -> None:
    """
    A round trip through the database must not alter the repository's identity.

    Comparing the two bodies whole, rather than field by field, also pins that
    `created_at` survives serialisation unchanged — a timestamp that lost its
    timezone or its microseconds on the way back out would fail here.

    """
    created = create_repository_body(client, f"{prefix}/backend-api")

    response = client.get(f"{REPOSITORIES_URL}/{created['id']}")

    assert response.status_code == 200
    assert response.json() == created


def test_retrieving_an_unknown_id_returns_404_not_found(client: TestClient) -> None:
    """
    An id that was never issued must be a clean `404`, not a lookup failure.

    """
    response = client.get(f"{REPOSITORIES_URL}/{uuid4()}")

    assert response.status_code == 404
    body = response.json()
    assert set(body) == ERROR_FIELDS
    assert body["code"] == "not_found"


def test_retrieving_a_soft_deleted_repository_returns_404_not_found(
    client: TestClient, prefix: str
) -> None:
    """A deleted repository must be indistinguishable from one that never existed.

    Same status, same `code`, same body shape as the unknown-id case above. If the
    two ever diverged — a different code, a message naming the deletion — a client
    could enumerate deleted repositories by comparing responses.
    """
    created = create_repository_body(client, f"{prefix}/wooloo")
    assert client.delete(f"{REPOSITORIES_URL}/{created['id']}").status_code == 204

    response = client.get(f"{REPOSITORIES_URL}/{created['id']}")

    assert response.status_code == 404
    body = response.json()
    assert set(body) == ERROR_FIELDS
    assert body["code"] == "not_found"


# ---------------------------------------------------------------------------
# DELETE /api/v1/repositories/{repository_id}
# ---------------------------------------------------------------------------


def test_deleting_a_repository_returns_204_with_a_genuinely_empty_body(
    client: TestClient, prefix: str
) -> None:
    """A successful delete must return no content and claim no content type.

    The header assertion is the non-obvious half. FastAPI's default
    `JSONResponse` stamps `content-type: application/json` onto a `204` even
    though it carries no body, which some strict clients reject; the route
    declares `response_class=Response` precisely to avoid that, and this is what
    holds that decision in place.
    """
    created = create_repository_body(client, f"{prefix}/wooloo")

    response = client.delete(f"{REPOSITORIES_URL}/{created['id']}")

    assert response.status_code == 204
    assert response.text == ""
    assert "content-type" not in response.headers


def test_repeating_a_delete_returns_204_again(client: TestClient, prefix: str) -> None:
    """Deleting an already-deleted repository must succeed, not fail.

    The post-condition the caller asked for already holds, so a retried or
    duplicated delete is safe by design. Returning `404` on the second call would
    make a network retry look like a bug to the client.
    """
    created = create_repository_body(client, f"{prefix}/wooloo")

    first = client.delete(f"{REPOSITORIES_URL}/{created['id']}")
    second = client.delete(f"{REPOSITORIES_URL}/{created['id']}")

    assert first.status_code == 204
    assert second.status_code == 204
    assert second.text == ""


def test_deleting_an_unknown_id_returns_404_not_found(client: TestClient) -> None:
    """An id that never existed must be a `404`, even though delete is idempotent.

    Idempotence covers "already done", not "never was". Answering `204` here would
    tell a client its delete succeeded against a repository that was never there —
    most likely a typo or a stale id it should hear about.
    """
    response = client.delete(f"{REPOSITORIES_URL}/{uuid4()}")

    assert response.status_code == 404
    body = response.json()
    assert set(body) == ERROR_FIELDS
    assert body["code"] == "not_found"


# ---------------------------------------------------------------------------
# Correlation and logging
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ErrorCase:
    """One way to make this surface fail, and the answer it must give.

    Attributes:
        label: Identifier used for the pytest parameter id.
        trigger: Issues the failing request, given a client, the test's name
            prefix, and the headers to send. Every case is expressed as a real
            request against a real route rather than a raised exception, because
            what is under test is that the *route* reaches the *handler* — a
            synthetic app proving the handler works in isolation is Epic 1's job
            and would pass even if `main.py` never registered it.
        status_code: HTTP status the response must carry.
        code: Machine-readable `code` the body must carry.
    """

    label: str

    trigger: Callable[[TestClient, str, dict[str, str]], Response]

    status_code: int

    code: str


def _trigger_invalid_name(
    client: TestClient, _prefix: str, headers: dict[str, str]
) -> Response:
    """Attempt a name the OCI grammar rejects.

    Args:
        client: The client to issue the request through.
        _prefix: Unused — an invalid name cannot carry a valid prefix.
        headers: Headers to send.

    Returns:
        The `400` response.
    """
    return client.post(REPOSITORIES_URL, json={"name": "Library/Nginx"}, headers=headers)


def _trigger_conflict(client: TestClient, prefix: str, headers: dict[str, str]) -> Response:
    """Register a name, then attempt it again.

    Args:
        client: The client to issue the request through.
        prefix: The test's private namespace, so the name is free to begin with.
        headers: Headers to send, on the second request only — the first must not
            share the correlation ID under test, or a handler that echoed the
            *previous* request's ID would still look correct.

    Returns:
        The `409` response.
    """
    name = f"{prefix}/backend-api"
    create_repository_body(client, name)

    return client.post(REPOSITORIES_URL, json={"name": name}, headers=headers)


def _trigger_not_found(client: TestClient, _prefix: str, headers: dict[str, str]) -> Response:
    """Look up an id that was never issued.

    Args:
        client: The client to issue the request through.
        _prefix: Unused — no repository is created for this case.
        headers: Headers to send.

    Returns:
        The `404` response.
    """
    return client.get(f"{REPOSITORIES_URL}/{uuid4()}", headers=headers)


ERROR_CASES: Final[tuple[_ErrorCase, ...]] = (
    _ErrorCase("invalid-name", _trigger_invalid_name, 400, "validation_error"),
    _ErrorCase("not-found", _trigger_not_found, 404, "not_found"),
    _ErrorCase("conflict", _trigger_conflict, 409, "conflict"),
)
"""
Every failure the repository domain can produce, one per registered handler.

"""


@pytest.mark.parametrize("case", ERROR_CASES, ids=lambda case: case.label)
@pytest.mark.parametrize("inbound", [None, SUPPLIED_REQUEST_ID], ids=["generated", "supplied"])
def test_a_domain_error_body_is_correlated_to_the_response_header(
    client: TestClient, prefix: str, case: _ErrorCase, inbound: str | None
) -> None:
    """Every repository error must be traceable from the client's own response.

    Args:
        case: The failure to provoke and the answer it must give.
        inbound: The `X-Request-ID` to send, or `None` to send no header.
    """
    headers = {} if inbound is None else {"X-Request-ID": inbound}

    response = case.trigger(client, prefix, headers)

    issued = response.headers["x-request-id"]
    assert response.status_code == case.status_code
    assert issued
    assert response.json()["code"] == case.code
    assert response.json()["request_id"] == issued

    if inbound is None:
        assert issued != SUPPLIED_REQUEST_ID
    else:
        assert issued == inbound


@pytest.mark.parametrize("case", ERROR_CASES, ids=lambda case: case.label)
def test_a_domain_error_logs_a_warning_with_no_traceback(
    client: TestClient, captured_logs: list[EventDict], prefix: str, case: _ErrorCase
) -> None:
    """A repository error is an expected outcome, and must be logged as one.

    Args:
        case: The failure to provoke and the `code` its log line must carry.
    """
    response = case.trigger(client, prefix, {})

    record = single_event(captured_logs, FAILURE_EVENT)
    assert record["log_level"] == "warning"
    assert record["code"] == case.code
    assert record["request_id"] == response.headers["x-request-id"]
    assert "exception" not in record
    assert "exc_info" not in record


def test_creating_a_repository_logs_repository_created(
    client: TestClient, captured_logs: list[EventDict], prefix: str
) -> None:
    """
    A write must leave one structured, correlated record naming what it wrote.

    The logged id is asserted against the id in the response body rather than
    against "some UUID", so a record that named a different row — or reported the
    requested name instead of the stored one — cannot pass.

    """
    name = f"{prefix}/backend-api"

    response = create_repository(client, name)

    assert response.status_code == 201
    record = single_event(captured_logs, CREATED_EVENT)
    assert record["log_level"] == "info"
    assert record["repository_id"] == response.json()["id"]
    assert record["repository_name"] == name
    assert record["request_id"] == response.headers["x-request-id"]


@pytest.mark.parametrize("event", [RETRIEVED_EVENT, DELETED_EVENT])
def test_a_read_and_a_delete_each_log_their_own_correlated_event(
    client: TestClient, captured_logs: list[EventDict], prefix: str, event: str
) -> None:
    """Retrieval and deletion must each be observable, and attributable.

    Args:
        event: The structlog event the operation under test must emit.
    """
    created = create_repository_body(client, f"{prefix}/backend-api")
    url = f"{REPOSITORIES_URL}/{created['id']}"

    response = client.get(url) if event == RETRIEVED_EVENT else client.delete(url)

    assert response.status_code in {200, 204}
    record = single_event(captured_logs, event)
    assert record["log_level"] == "info"
    assert record["repository_id"] == created["id"]
    assert record["request_id"] == response.headers["x-request-id"]


def test_listing_emits_no_per_call_domain_event(
    client: TestClient, captured_logs: list[EventDict], prefix: str
) -> None:
    """Listing must stay silent, leaving only the middleware's request summary.

    The omission is deliberate — a log line per list call would scale with read
    traffic and carry nothing an operator would act on — so it is worth pinning:
    without this test, someone "fixing the missing log" would look right.
    """
    create_repository_body(client, f"{prefix}/backend-api")
    captured_logs.clear()

    assert client.get(f"{REPOSITORIES_URL}?limit=100").status_code == 200

    assert [event["event"] for event in captured_logs] == ["http_request"]
