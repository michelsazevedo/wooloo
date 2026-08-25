"""
Unit tests for `DeleteRepositoryUseCase`.

Deletion is idempotent by contract, and the whole design rests on what the store's
boolean means: it answers "does this id exist at all", not "did this call change a
row". These tests pin the two-way translation that sits on top of it — `True`
becomes a logged success, `False` becomes `RepositoryNotFound` — and, in
particular, that a repeat delete is indistinguishable from a first one in the log.

The store is doubled at the `RepositoryStore` port (see `_fakes`), which is the
right seam here for a reason beyond convenience: whether the row was already
soft-deleted is information the port deliberately withholds, so a double that can
only answer `True`/`False` reproduces exactly the knowledge this use case has.

"""

from uuid import UUID

import pytest
from _fakes import FakeRepositoryStore
from structlog.typing import EventDict

from wooloo.application.use_cases.delete_repository import DeleteRepositoryUseCase
from wooloo.domain.repositories.exceptions import RepositoryNotFound

REPOSITORY_ID = UUID("33333333-3333-4333-8333-333333333333")
"""The id under deletion."""


def repository_deleted_events(events: list[EventDict]) -> list[EventDict]:
    """Select the deletion events from everything captured during a test.

    Args:
        events: Every structlog event the test captured, in order.

    Returns:
        Only the `repository_deleted` events, in order.
    """
    return [event for event in events if event["event"] == "repository_deleted"]


async def test_execute_succeeds_when_the_store_reports_the_id_exists() -> None:
    """`True` from the store is a success: the delete goes through and nothing raises.

    That the call reports nothing back is *not* asserted here, deliberately.
    `execute` is annotated `-> None`, so binding its result is a type error rather
    than a test — `mypy src` already forbids any caller from reading a value, and a
    runtime `is None` check would only restate what the annotation guarantees while
    failing type checking itself.
    """
    store = FakeRepositoryStore(delete_returns=True)

    await DeleteRepositoryUseCase(store).execute(REPOSITORY_ID)

    assert store.delete_calls == [REPOSITORY_ID]


async def test_execute_logs_the_deletion(captured_logs: list[EventDict]) -> None:
    """`repository_deleted` records the id, as a string, exactly once.

    Only the id is available: the store returns a bare boolean, so logging a name
    would require a second query the request no longer needs.
    """
    store = FakeRepositoryStore(delete_returns=True)

    await DeleteRepositoryUseCase(store).execute(REPOSITORY_ID)

    deleted = repository_deleted_events(captured_logs)
    assert len(deleted) == 1
    assert deleted[0]["repository_id"] == str(REPOSITORY_ID)
    assert isinstance(deleted[0]["repository_id"], str)
    assert deleted[0]["log_level"] == "info"


async def test_a_repeat_delete_succeeds_and_logs_identically(
    captured_logs: list[EventDict],
) -> None:
    """Deleting twice is two successes that are indistinguishable in the log.

    Both calls get `True` from the store — that is what `True` means, "this id
    exists, deleted now or deleted earlier" — so both must take the same path and
    produce the same record. Equality of the two captured events, rather than mere
    presence of a second one, is what pins it: an implementation that grew a
    `already_deleted=True` field, downgraded the repeat to `debug`, renamed its
    event, or skipped logging the retry altogether would satisfy "a second event
    exists" and fail here.

    The property matters operationally. A client that never saw the first response
    retries; if only the "real" deletion were logged, half the delete traffic — and
    an entire retry storm — would be invisible to an operator reading these events.
    """
    store = FakeRepositoryStore(delete_returns=True)
    use_case = DeleteRepositoryUseCase(store)

    await use_case.execute(REPOSITORY_ID)
    await use_case.execute(REPOSITORY_ID)

    deleted = repository_deleted_events(captured_logs)
    assert len(deleted) == 2
    assert deleted[0] == deleted[1]
    assert captured_logs == deleted
    assert store.delete_calls == [REPOSITORY_ID, REPOSITORY_ID]


async def test_execute_raises_not_found_and_logs_nothing_when_the_id_never_existed(
    captured_logs: list[EventDict],
) -> None:
    """`False` from the store is a genuine miss, and nothing is recorded as deleted.

    There is no post-condition to satisfy because there is no repository, so this
    is the one delete outcome that is not a success. Logging `repository_deleted`
    here would put a deletion in the record for a repository that never existed,
    which is worse than silence: it is a false one.
    """
    store = FakeRepositoryStore(delete_returns=False)

    with pytest.raises(RepositoryNotFound) as raised:
        await DeleteRepositoryUseCase(store).execute(REPOSITORY_ID)

    assert str(REPOSITORY_ID) in str(raised.value)
    assert store.delete_calls == [REPOSITORY_ID]
    assert captured_logs == []


async def test_execute_does_not_re_check_existence_around_the_delete() -> None:
    """One atomic store call, with no existence check before or after it.

    Reading the row and then deleting it would recover the same information while
    opening a window for a concurrent delete to land in between — turning a valid
    request into a 404 under exactly the concurrency the boolean return exists to
    tolerate. Nothing about the use case's own inputs or outputs reveals such a
    read, so the empty read-call lists are the only evidence available.
    """
    store = FakeRepositoryStore(delete_returns=True)

    await DeleteRepositoryUseCase(store).execute(REPOSITORY_ID)

    assert store.get_by_id_calls == []
    assert store.get_by_name_calls == []
    assert len(store.delete_calls) == 1
