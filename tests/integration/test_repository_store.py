"""
Integration tests for `SqlAlchemyRepositoryStore` against the real PostgreSQL.

"""

from collections.abc import AsyncIterator
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from wooloo.domain.repositories.entity import Repository
from wooloo.domain.repositories.exceptions import RepositoryAlreadyExists
from wooloo.infrastructure.database.engine import dispose_engine, get_session_factory
from wooloo.infrastructure.database.models.repository import RepositoryModel
from wooloo.infrastructure.repositories.store import SqlAlchemyRepositoryStore

UNBOUNDED_LIMIT = 1_000_000
"""
A `limit` large enough that a page holds every active row.

"""

CONCURRENT_WRITER_HINT = (
    "the listing changed shape mid-test, which means something outside this test "
    "wrote to the shared `repositories` table while it ran — see this module's "
    "docstring: the listing assertions require the suite to run serially against a "
    "database no other suite is writing to at the same time"
)
"""
Diagnostic for the one failure mode the listing tests cannot design away.

Attached to the assertions that read the unfiltered global listing, so that a
failure caused by a second suite sharing the database reports its actual cause
instead of an inscrutable diff between two lists of repositories.

"""


def newest_first(repositories: list[Repository]) -> list[Repository]:
    """Order repositories the way `SqlAlchemyRepositoryStore.list` contracts to.

    Args:
        repositories: The repositories to order, in any order.

    Returns:
        The same repositories, newest created first, ties broken on descending id.
    """
    return sorted(repositories, key=lambda entity: (entity.created_at, entity.id), reverse=True)


class StoreHarness:
    """One test's private namespace in the shared `repositories` table.

    Attributes:
        namespace: The random name prefix owned by this test, e.g. `qa-3f2b...`.
            Every name `name()` produces starts with it, which is what makes the
            teardown's prefixed delete exhaustive.
    """

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], namespace: str
    ) -> None:
        """Initialize the harness.

        Args:
            session_factory: Factory producing sessions bound to the real engine.
            namespace: The name prefix this test owns.
        """
        self._session_factory = session_factory
        self._sessions: list[AsyncSession] = []
        self.namespace = namespace

    def name(self, suffix: str) -> str:
        """Mint a repository name inside this test's namespace.

        Args:
            suffix: A short, human-readable path component naming what the row is
                for, e.g. `duplicate`.

        Returns:
            A namespaced repository name, e.g. `qa-3f2b.../duplicate`.
        """
        return f"{self.namespace}/{suffix}"

    def new_session(self) -> AsyncSession:
        """Open a session that the teardown will close.

        Returns:
            A new session bound to the real engine, registered for cleanup.
        """
        session = self._session_factory()
        self._sessions.append(session)
        return session

    def new_store(self) -> SqlAlchemyRepositoryStore:
        """Build a store on a session of its own.

        Returns:
            A store over a fresh session.
        """
        return SqlAlchemyRepositoryStore(self.new_session())

    async def read_deleted_at(self, repository_id: UUID) -> datetime | None:
        """Read a row's `deleted_at` directly, bypassing the store and the ORM cache.

        Args:
            repository_id: The id of a row known to exist.

        Returns:
            The row's soft-deletion timestamp, or `None` if it is still active.
        """
        statement = select(RepositoryModel.deleted_at).where(
            RepositoryModel.id == repository_id
        )
        async with self._session_factory() as session:
            return (await session.execute(statement)).scalar_one()

    async def aclose(self) -> None:
        """
        Close every session handed out, releasing their transactions and connections.

        """
        for session in self._sessions:
            await session.close()


async def purge(session_factory: async_sessionmaker[AsyncSession], namespace: str) -> None:
    """Hard-delete every row a test created.

    Args:
        session_factory: Factory producing sessions bound to the real engine.
        namespace: The test's name prefix.
    """
    statement = delete(RepositoryModel).where(
        RepositoryModel.name.startswith(f"{namespace}/", autoescape=True)
    )
    async with session_factory() as session:
        await session.execute(statement)
        await session.commit()


@pytest.fixture
async def harness() -> AsyncIterator[StoreHarness]:
    """Give one test an isolated namespace, and leave the table as it was found.

    Yields:
        The harness, with cleanup ordered as close-sessions, purge, dispose — the
        purge needs a live engine, and it needs the test's own transactions ended
        first so it is not waiting on locks they hold.
    """
    session_factory = get_session_factory()
    harness = StoreHarness(session_factory, namespace=f"qa-{uuid4().hex}")

    try:
        yield harness
    finally:
        await harness.aclose()
        await purge(session_factory, harness.namespace)
        await dispose_engine()


@pytest.fixture
def store(harness: StoreHarness) -> SqlAlchemyRepositoryStore:
    """
    Return the store under test, on the session most tests need only one of.

    """
    return harness.new_store()


async def test_create_returns_a_fully_populated_entity(
    store: SqlAlchemyRepositoryStore, harness: StoreHarness
) -> None:
    """Every database-generated field must come back on the returned entity.

    `updated_at` equalling `created_at` is the contract for a row nothing has
    modified yet; a soft delete is the only thing that later moves them apart.
    """
    name = harness.name("fully-populated")

    created = await store.create(name)

    assert isinstance(created.id, UUID)
    assert created.name == name
    assert created.deleted_at is None
    assert created.created_at.tzinfo is not None
    assert created.updated_at.tzinfo is not None
    assert created.updated_at == created.created_at


async def test_create_commits_so_another_connection_can_read_the_row(
    store: SqlAlchemyRepositoryStore, harness: StoreHarness
) -> None:
    """
    A created repository must survive the session that created it.

    """
    created = await store.create(harness.name("committed"))

    observer = harness.new_store()

    assert await observer.get_by_id(created.id) == created


async def test_create_rejects_a_duplicate_name(
    store: SqlAlchemyRepositoryStore, harness: StoreHarness
) -> None:
    """
    A name is registry-wide unique, and the second claim on it is a domain error
    rather than the raw `IntegrityError` the database raised.

    """
    name = harness.name("duplicate")
    await store.create(name)

    with pytest.raises(RepositoryAlreadyExists):
        await store.create(name)


async def test_create_rejects_a_name_differing_only_in_case(
    store: SqlAlchemyRepositoryStore, harness: StoreHarness
) -> None:
    """
    `CITEXT` must make case a non-difference at insert time.

    """
    name = harness.name("case-folded")
    await store.create(name)

    with pytest.raises(RepositoryAlreadyExists):
        await store.create(name.upper())


async def test_create_rejects_a_name_still_held_by_a_soft_deleted_repository(
    store: SqlAlchemyRepositoryStore, harness: StoreHarness
) -> None:
    """
    A soft-deleted repository keeps its name.

    """
    name = harness.name("still-taken")
    created = await store.create(name)
    assert await store.delete(created.id) is True

    with pytest.raises(RepositoryAlreadyExists):
        await store.create(name)


async def test_session_is_reusable_after_a_duplicate_name_conflict(
    store: SqlAlchemyRepositoryStore, harness: StoreHarness
) -> None:
    """
    A caught conflict must not poison the caller's session.

    """
    name = harness.name("conflict-then-read")
    original = await store.create(name)

    with pytest.raises(RepositoryAlreadyExists):
        await store.create(name)

    assert await store.get_by_name(name) == original


async def test_get_by_id_and_get_by_name_find_the_created_repository(
    store: SqlAlchemyRepositoryStore, harness: StoreHarness
) -> None:
    """
    Both lookups must resolve to the same entity `create` returned.

    """
    created = await store.create(harness.name("findable"))

    assert await store.get_by_id(created.id) == created
    assert await store.get_by_name(created.name) == created


async def test_get_by_name_matches_regardless_of_case(
    store: SqlAlchemyRepositoryStore, harness: StoreHarness
) -> None:
    """
    Lookup must agree with the unique index on what counts as the same name.

    """
    created = await store.create(harness.name("case-insensitive-lookup"))

    assert await store.get_by_name(created.name.upper()) == created


async def test_get_by_id_returns_none_for_an_id_that_never_existed(
    store: SqlAlchemyRepositoryStore,
) -> None:
    """
    A miss is `None`, not an exception — turning absence into `RepositoryNotFound`
    is the use case's decision, not the store's.

    """
    assert await store.get_by_id(uuid4()) is None


async def test_get_by_name_returns_none_for_a_name_that_never_existed(
    store: SqlAlchemyRepositoryStore, harness: StoreHarness
) -> None:
    """
    The same contract, keyed by name.

    """
    assert await store.get_by_name(harness.name("never-created")) is None


async def test_deleted_repository_is_invisible_to_every_read_path(
    store: SqlAlchemyRepositoryStore, harness: StoreHarness
) -> None:
    """
    A soft-deleted row must be indistinguishable from one that never existed.

    """
    created = await store.create(harness.name("vanishing"))
    before = await store.list(limit=UNBOUNDED_LIMIT, offset=0)

    assert await store.delete(created.id) is True

    after = await store.list(limit=UNBOUNDED_LIMIT, offset=0)

    assert await store.get_by_id(created.id) is None
    assert await store.get_by_name(created.name) is None
    assert created.id in {item.id for item in before.items}
    assert created.id not in {item.id for item in after.items}


async def test_list_pages_through_repositories_newest_created_first(
    store: SqlAlchemyRepositoryStore, harness: StoreHarness
) -> None:
    """
    Paging must return disjoint, correctly ordered slices of one stable order.

    """
    created = [await store.create(harness.name(f"page-{index}")) for index in range(5)]
    expected = newest_first(created)
    mine = {entity.id for entity in created}

    whole = await store.list(limit=UNBOUNDED_LIMIT, offset=0)
    base = next(index for index, item in enumerate(whole.items) if item.id in mine)

    first = await store.list(limit=2, offset=base)
    second = await store.list(limit=2, offset=base + 2)
    last = await store.list(limit=1, offset=base + 4)

    timestamps = [item.created_at for item in whole.items]

    assert timestamps == sorted(timestamps, reverse=True)
    assert whole.items[base : base + 5] == expected, CONCURRENT_WRITER_HINT
    assert first.items == expected[:2]
    assert second.items == expected[2:4]
    assert last.items == expected[4:]
    assert whole.total == len(whole.items), CONCURRENT_WRITER_HINT


async def test_list_echoes_back_the_limit_and_offset_it_was_given(
    store: SqlAlchemyRepositoryStore,
) -> None:
    """
    The page reports the window that produced it, unclamped.

    """
    page = await store.list(limit=3, offset=7)

    assert page.limit == 3
    assert page.offset == 7


async def test_list_excludes_soft_deleted_rows_from_items_and_total(
    store: SqlAlchemyRepositoryStore, harness: StoreHarness
) -> None:
    """
    `total` counts live repositories, not rows.

    """
    kept = await store.create(harness.name("kept"))
    removed = await store.create(harness.name("removed"))
    before = await store.list(limit=UNBOUNDED_LIMIT, offset=0)

    assert await store.delete(removed.id) is True

    after = await store.list(limit=UNBOUNDED_LIMIT, offset=0)

    assert removed.id in {item.id for item in before.items}
    assert removed.id not in {item.id for item in after.items}
    assert kept.id in {item.id for item in after.items}
    assert after.total == len(after.items), CONCURRENT_WRITER_HINT


async def test_list_returns_an_empty_page_when_the_offset_is_past_the_end(
    store: SqlAlchemyRepositoryStore, harness: StoreHarness
) -> None:
    """
    Running off the end is an ordinary empty page, never an error.

    """
    await store.create(harness.name("only-row"))
    populated = await store.list(limit=UNBOUNDED_LIMIT, offset=0)

    page = await store.list(limit=10, offset=populated.total + 1)

    assert page.items == []
    assert page.total > 0
    assert page.limit == 10
    assert page.offset == populated.total + 1


async def test_list_breaks_created_at_ties_on_descending_id(harness: StoreHarness) -> None:
    """
    Rows sharing a `created_at` must still come back in a stable, defined order.

    """
    shared = uuid4().hex[:30]
    lower_id = UUID(f"{shared}00")
    higher_id = UUID(f"{shared}ff")

    session = harness.new_session()
    session.add_all(
        [
            RepositoryModel(id=lower_id, name=harness.name("tie-lower-id")),
            RepositoryModel(id=higher_id, name=harness.name("tie-higher-id")),
        ]
    )
    await session.flush()
    await session.commit()

    page = await harness.new_store().list(limit=UNBOUNDED_LIMIT, offset=0)
    tied = [item for item in page.items if item.name.startswith(harness.namespace)]

    assert len(tied) == 2
    assert tied[0].created_at == tied[1].created_at
    assert [item.id for item in tied] == [higher_id, lower_id]


async def test_delete_reports_true_for_a_repository_that_exists(
    store: SqlAlchemyRepositoryStore, harness: StoreHarness
) -> None:
    """
    `True` is the caller's signal to answer 204 rather than 404.

    """
    created = await store.create(harness.name("deletable"))

    assert await store.delete(created.id) is True


async def test_delete_is_idempotent_and_never_rewrites_the_original_deleted_at(
    store: SqlAlchemyRepositoryStore, harness: StoreHarness
) -> None:
    """
    A repeat delete succeeds again without moving the deletion moment.

    """
    created = await store.create(harness.name("twice-deleted"))

    assert await store.delete(created.id) is True
    first_deleted_at = await harness.read_deleted_at(created.id)

    assert await store.delete(created.id) is True
    second_deleted_at = await harness.read_deleted_at(created.id)

    assert first_deleted_at is not None
    assert second_deleted_at == first_deleted_at


async def test_delete_returns_false_for_an_id_that_never_existed(
    store: SqlAlchemyRepositoryStore,
) -> None:
    """
    An unknown id is reported by the return value, not by an exception.

    """
    assert await store.delete(uuid4()) is False
