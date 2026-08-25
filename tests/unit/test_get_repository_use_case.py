"""
Unit tests for `GetRepositoryUseCase`.

Both lookups share one contract with two halves. On a hit the entity is returned
untouched and a `repository_retrieved` event describes the row that was found; on a
miss the store's `None` becomes `RepositoryNotFound` and **nothing is logged** —
a miss is the caller's problem to report, not an event worth a line in every
operator's log at 404 volume.

The store is doubled at the `RepositoryStore` port (see `_fakes`), so a miss here
is the port's `None`, exactly what a soft-deleted or never-created row produces in
production.

"""

from uuid import UUID

import pytest
from _fakes import FakeRepositoryStore, make_repository
from structlog.typing import EventDict

from wooloo.application.use_cases.get_repository import GetRepositoryUseCase
from wooloo.domain.repositories.exceptions import RepositoryNotFound

REQUESTED_ID = UUID("11111111-1111-4111-8111-111111111111")
"""The id a caller looks up."""

PERSISTED_ID = UUID("22222222-2222-4222-8222-222222222222")
"""
The id carried by the entity the store double returns.

Deliberately different from `REQUESTED_ID`. The logged `repository_id` must come
from the entity that was found, and a double echoing the requested id back would
make the two sources indistinguishable — every assertion would pass whether the
implementation read the entity or its own argument.
"""

REQUESTED_NAME = "acme/backend-api"
"""The name a caller looks up."""

PERSISTED_NAME = "stored/by-the-database"
"""The name carried by the returned entity, different from the requested one for
the same reason `PERSISTED_ID` is."""


def repository_retrieved_events(events: list[EventDict]) -> list[EventDict]:
    """Select the retrieval events from everything captured during a test.

    Args:
        events: Every structlog event the test captured, in order.

    Returns:
        Only the `repository_retrieved` events, in order.
    """
    return [event for event in events if event["event"] == "repository_retrieved"]


async def test_by_id_returns_the_entity_the_store_found() -> None:
    """A hit hands back the store's entity itself, not a copy."""
    found = make_repository(PERSISTED_NAME, repository_id=PERSISTED_ID)
    store = FakeRepositoryStore(get_by_id_returns=found)

    result = await GetRepositoryUseCase(store).by_id(REQUESTED_ID)

    assert result is found
    assert store.get_by_id_calls == [REQUESTED_ID]


async def test_by_id_logs_the_repository_it_found(captured_logs: list[EventDict]) -> None:
    """`repository_retrieved` describes the row found, not the key looked up.

    The double returns an entity whose id and name both differ from the request, so
    an implementation logging its own `repository_id` argument would emit
    `REQUESTED_ID` and fail here.
    """
    found = make_repository(PERSISTED_NAME, repository_id=PERSISTED_ID)
    store = FakeRepositoryStore(get_by_id_returns=found)

    await GetRepositoryUseCase(store).by_id(REQUESTED_ID)

    retrieved = repository_retrieved_events(captured_logs)
    assert len(retrieved) == 1
    assert retrieved[0]["repository_id"] == str(PERSISTED_ID)
    assert retrieved[0]["repository_id"] != str(REQUESTED_ID)
    assert retrieved[0]["repository_name"] == PERSISTED_NAME
    assert retrieved[0]["log_level"] == "info"


async def test_by_id_raises_not_found_and_logs_nothing_on_a_miss(
    captured_logs: list[EventDict],
) -> None:
    """A miss becomes the domain error and produces no log output at all.

    The empty-log assertion is not incidental tidiness: retrieval misses occur at
    the rate clients guess wrong, so logging one per miss would let an enumeration
    scan flood the log. The message must still carry the id that missed, since the
    exception is the only record of what was actually asked for.
    """
    store = FakeRepositoryStore(get_by_id_returns=None)

    with pytest.raises(RepositoryNotFound) as raised:
        await GetRepositoryUseCase(store).by_id(REQUESTED_ID)

    assert str(REQUESTED_ID) in str(raised.value)
    assert store.get_by_id_calls == [REQUESTED_ID]
    assert captured_logs == []


async def test_by_name_returns_the_entity_the_store_found() -> None:
    """A hit hands back the store's entity itself, and the name goes down verbatim.

    Case-insensitive matching is the store's job, delegated deliberately so lookup
    and the unique constraint agree on what counts as the same name. The use case
    must therefore not normalize, case-fold or re-validate on the way past — the
    mixed-case input is asserted to arrive at the port unchanged.
    """
    found = make_repository(PERSISTED_NAME, repository_id=PERSISTED_ID)
    store = FakeRepositoryStore(get_by_name_returns=found)

    result = await GetRepositoryUseCase(store).by_name("Acme/Backend-API")

    assert result is found
    assert store.get_by_name_calls == ["Acme/Backend-API"]


async def test_by_name_logs_the_repository_it_found(captured_logs: list[EventDict]) -> None:
    """`repository_retrieved` reports the found row, with the same fields as `by_id`.

    Both lookups share one logging helper precisely so their events cannot drift
    apart; asserting the identical shape here is what would fail if one call site
    grew a field the other lacks.
    """
    found = make_repository(PERSISTED_NAME, repository_id=PERSISTED_ID)
    store = FakeRepositoryStore(get_by_name_returns=found)

    await GetRepositoryUseCase(store).by_name(REQUESTED_NAME)

    retrieved = repository_retrieved_events(captured_logs)
    assert len(retrieved) == 1
    assert retrieved[0]["repository_id"] == str(PERSISTED_ID)
    assert retrieved[0]["repository_name"] == PERSISTED_NAME
    assert retrieved[0]["repository_name"] != REQUESTED_NAME
    assert retrieved[0]["log_level"] == "info"


async def test_by_name_raises_not_found_and_logs_nothing_on_a_miss(
    captured_logs: list[EventDict],
) -> None:
    """A name miss behaves exactly like an id miss: raise, log nothing.

    The name is repr-quoted in the message so a value that is empty or padded with
    whitespace is still visible in the output rather than vanishing into it.
    """
    store = FakeRepositoryStore(get_by_name_returns=None)

    with pytest.raises(RepositoryNotFound) as raised:
        await GetRepositoryUseCase(store).by_name(REQUESTED_NAME)

    assert repr(REQUESTED_NAME) in str(raised.value)
    assert store.get_by_name_calls == [REQUESTED_NAME]
    assert captured_logs == []


async def test_the_two_lookups_do_not_reach_each_other_s_store_method() -> None:
    """`by_id` and `by_name` query the port by the key they were given.

    Guards the split the class is built around: one method per key, dispatched
    statically. A `by_name` that fell through to `get_by_id` — or a shared helper
    that guessed from the argument's runtime type — would still return the double's
    entity and pass every assertion above.
    """
    found = make_repository(PERSISTED_NAME, repository_id=PERSISTED_ID)

    by_id_store = FakeRepositoryStore(get_by_id_returns=found)
    await GetRepositoryUseCase(by_id_store).by_id(REQUESTED_ID)

    by_name_store = FakeRepositoryStore(get_by_name_returns=found)
    await GetRepositoryUseCase(by_name_store).by_name(REQUESTED_NAME)

    assert by_id_store.get_by_name_calls == []
    assert by_name_store.get_by_id_calls == []
