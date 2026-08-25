"""
Unit tests for `CreateRepositoryUseCase`.

The store is a double at the `RepositoryStore` port (see `_fakes`); no database,
no session, no patched internals. What is under test is the orchestration the use
case owns and nothing else: that validation happens *before* persistence, that the
name reaching the store is the validated one, that the log records the row that was
actually written, and that a uniqueness conflict travels outward untouched.

"""

import pytest
from _fakes import FakeRepositoryStore, make_repository
from structlog.typing import EventDict

from wooloo.application.use_cases.create_repository import CreateRepositoryUseCase
from wooloo.domain.repositories.exceptions import (
    InvalidRepositoryName,
    RepositoryAlreadyExists,
)

REQUESTED_NAME = "acme/backend-api"
"""The name a caller asks to register."""

PERSISTED_NAME = "stored/by-the-database"
"""
The name the store double reports back.

Deliberately different from `REQUESTED_NAME`. The use case logs fields taken from
the returned entity, not from its own input parameter, and those two are
indistinguishable in a test where the double echoes the name it was given. Making
them disagree is what turns "the log has a name in it" into "the log has *this*
name in it", and is the only reason a regression that logs the input instead of
the row could ever fail a test here.
"""

INVALID_NAMES = [
    "Library/Nginx",
    "acme//backend",
    "/leading-slash",
    "trailing-slash/",
    "has whitespace",
    "acme/-leading-hyphen",
    "",
    "   ",
]
"""Names the OCI grammar rejects, one per rule `RepositoryName` enforces."""


def repository_created_events(events: list[EventDict]) -> list[EventDict]:
    """Select the creation events from everything captured during a test.

    Args:
        events: Every structlog event the test captured, in order.

    Returns:
        Only the `repository_created` events, in order.
    """
    return [event for event in events if event["event"] == "repository_created"]


async def test_execute_persists_the_validated_name() -> None:
    """The store receives the *validated* name, not the caller's raw string.

    `RepositoryName` strips surrounding whitespace, so a padded input must reach
    persistence stripped. Passing `name` straight through would store a name no
    lookup could ever match, since every later read compares against the stripped
    form.
    """
    store = FakeRepositoryStore(create_returns=make_repository(PERSISTED_NAME))

    await CreateRepositoryUseCase(store).execute(f"  {REQUESTED_NAME}  ")

    assert store.create_calls == [REQUESTED_NAME]


async def test_execute_returns_the_stored_entity() -> None:
    """The caller gets back the store's entity itself, not a reconstruction of it.

    Only the persisted row carries the database-assigned id and timestamps, so
    rebuilding an entity from the input name here would hand the caller values the
    database never issued.
    """
    persisted = make_repository(PERSISTED_NAME)
    store = FakeRepositoryStore(create_returns=persisted)

    result = await CreateRepositoryUseCase(store).execute(REQUESTED_NAME)

    assert result is persisted


async def test_execute_logs_the_persisted_entity_rather_than_the_input(
    captured_logs: list[EventDict],
) -> None:
    """`repository_created` reports the row that was written, not what was asked for.

    The double returns an entity whose name differs from the requested one, so the
    two possible sources for `repository_name` produce different output. An
    implementation that logged its own `name` parameter — the easy, plausible
    regression — would emit `acme/backend-api` here and fail. The id assertion is
    load-bearing for the same reason from the other direction: an id exists only on
    the persisted row, so it can only have come from the entity.
    """
    persisted = make_repository(PERSISTED_NAME)
    store = FakeRepositoryStore(create_returns=persisted)

    await CreateRepositoryUseCase(store).execute(REQUESTED_NAME)

    created = repository_created_events(captured_logs)
    assert len(created) == 1
    assert created[0]["repository_name"] == PERSISTED_NAME
    assert created[0]["repository_name"] != REQUESTED_NAME
    assert created[0]["repository_id"] == str(persisted.id)
    assert created[0]["log_level"] == "info"


async def test_execute_logs_ids_as_strings(captured_logs: list[EventDict]) -> None:
    """The id is rendered as a string, keeping the event JSON-serializable.

    A raw `UUID` survives an in-memory capture unnoticed but is not JSON, so the
    production renderer is where it would surface. Pinning the type here catches it
    at the call site instead.
    """
    store = FakeRepositoryStore(create_returns=make_repository(PERSISTED_NAME))

    await CreateRepositoryUseCase(store).execute(REQUESTED_NAME)

    assert isinstance(repository_created_events(captured_logs)[0]["repository_id"], str)


@pytest.mark.parametrize("invalid", INVALID_NAMES, ids=repr)
async def test_execute_rejects_an_invalid_name_without_touching_the_store(
    invalid: str, captured_logs: list[EventDict]
) -> None:
    """An illegal name never becomes a round trip, a row, or a log line.

    This is the ordering guarantee the use case exists to make: validation happens
    before the store is reached. Asserting the raised exception alone would not
    pin it — a use case that inserted first and validated afterwards would raise
    exactly the same error while having already spent a transaction on input the
    grammar could have rejected for free. The empty call list is what makes the
    ordering observable, and the empty log list confirms nothing was recorded as
    created.
    """
    store = FakeRepositoryStore(create_returns=make_repository(PERSISTED_NAME))

    with pytest.raises(InvalidRepositoryName):
        await CreateRepositoryUseCase(store).execute(invalid)

    assert store.create_calls == []
    assert captured_logs == []


async def test_execute_propagates_repository_already_exists_unchanged(
    captured_logs: list[EventDict],
) -> None:
    """A uniqueness conflict reaches the caller as the store's own exception object.

    Identity — `is`, not `isinstance` — is the assertion that matters. Catching and
    re-raising a fresh `RepositoryAlreadyExists` would satisfy a type check while
    discarding the store's message and the original traceback, so the API layer
    would report a conflict it can no longer describe.
    """
    conflict = RepositoryAlreadyExists(f"repository already exists: {REQUESTED_NAME}")
    store = FakeRepositoryStore(create_raises=conflict)

    with pytest.raises(RepositoryAlreadyExists) as raised:
        await CreateRepositoryUseCase(store).execute(REQUESTED_NAME)

    assert raised.value is conflict
    assert store.create_calls == [REQUESTED_NAME]
    assert captured_logs == []
