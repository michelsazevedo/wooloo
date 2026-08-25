"""
An in-memory `RepositoryStore` double, shared by the four use-case unit test modules.

"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from wooloo.domain.repositories.contracts import RepositoryPage, RepositoryStore
from wooloo.domain.repositories.entity import Repository

FIXED_CREATED_AT = datetime(2026, 1, 15, 9, 30, tzinfo=UTC)
"""A fixed, timezone-aware creation moment.

Entities are built from constants rather than `datetime.now()`: nothing under test
reads a timestamp, so a moving clock could only introduce nondeterminism without
buying a single additional assertion.
"""

FIXED_UPDATED_AT = FIXED_CREATED_AT


def make_repository(
    name: str,
    *,
    repository_id: UUID | None = None,
    deleted_at: datetime | None = None,
) -> Repository:
    """Build a `Repository` for a test to hand back through the store double.

    Args:
        name: The repository's name. Tests deliberately give the entity a name
            that differs from whatever was passed *into* the use case, so an
            assertion can tell which of the two a log line actually read.
        repository_id: The entity's id. Defaults to a UUIDv5 derived from `name`,
            which is stable across runs — a `uuid4()` default would make failure
            output differ every run for no benefit — while still differing between
            entities, so a test cannot pass by confusing one for another.
        deleted_at: Soft-deletion moment, `None` for an active repository.

    Returns:
        A fully populated entity with fixed timestamps.
    """
    return Repository(
        id=repository_id if repository_id is not None else uuid5(NAMESPACE_URL, name),
        name=name,
        created_at=FIXED_CREATED_AT,
        updated_at=FIXED_UPDATED_AT,
        deleted_at=deleted_at,
    )


@dataclass(frozen=True)
class PageRequest:
    """One recorded `RepositoryStore.list` call, as the store received it.

    Recorded as a value object rather than a `(limit, offset)` tuple so that an
    assertion reads `PageRequest(limit=100, offset=0)` and cannot silently pass
    with the two numbers transposed.

    Attributes:
        limit: The page size the store was actually called with — the *clamped*
            value when the use case bounded it.
        offset: The offset the store was actually called with, likewise post-clamp.
    """

    limit: int

    offset: int


class FakeRepositoryStore:
    """A `RepositoryStore` that records what it was asked and returns what it was told.

    Attributes:
        create_calls: Names passed to `create`, in order.
        get_by_id_calls: Ids passed to `get_by_id`, in order.
        get_by_name_calls: Names passed to `get_by_name`, in order.
        list_calls: `PageRequest`s the store was called with, in order.
        list_results: The `RepositoryPage` objects `list` returned, in order, so a
            test can assert the use case handed back the store's own page rather
            than a rebuilt copy of it.
        delete_calls: Ids passed to `delete`, in order.
    """

    def __init__(
        self,
        *,
        create_returns: Repository | None = None,
        create_raises: BaseException | None = None,
        get_by_id_returns: Repository | None = None,
        get_by_name_returns: Repository | None = None,
        page_items: list[Repository] | None = None,
        page_total: int = 0,
        delete_returns: bool | None = None,
    ) -> None:
        """Configure the double's answers.

        Args:
            create_returns: The entity `create` yields. Independent of the name it
                is called with, on purpose: a test proves which value a log line
                read by making the two disagree.
            create_raises: Raised by `create` instead of returning, used for the
                `RepositoryAlreadyExists` propagation case. The call is still
                recorded before the raise.
            get_by_id_returns: What `get_by_id` yields; `None` models a miss.
            get_by_name_returns: What `get_by_name` yields; `None` models a miss.
            page_items: Items placed on every page `list` returns. Copied on the
                way out, so a caller cannot mutate the double's configuration.
            page_total: The `total` reported on every page `list` returns.
            delete_returns: What `delete` reports. `True` means the id exists
                (whether this call deleted it or an earlier one did), `False` means
                it never existed. Left `None`, a `delete` call is an error.
        """
        self._create_returns = create_returns
        self._create_raises = create_raises
        self._get_by_id_returns = get_by_id_returns
        self._get_by_name_returns = get_by_name_returns
        self._page_items = page_items if page_items is not None else []
        self._page_total = page_total
        self._delete_returns = delete_returns

        self.create_calls: list[str] = []
        self.get_by_id_calls: list[UUID] = []
        self.get_by_name_calls: list[str] = []
        self.list_calls: list[PageRequest] = []
        self.list_results: list[RepositoryPage] = []
        self.delete_calls: list[UUID] = []

    async def create(self, name: str) -> Repository:
        """Record the name, then raise or return whatever was configured.

        Args:
            name: The name the use case passed down, already validated by it.

        Returns:
            The configured entity.

        Raises:
            BaseException: The configured `create_raises`, if any.
            AssertionError: If neither an outcome nor an error was configured,
                meaning the use case reached persistence unexpectedly.
        """
        self.create_calls.append(name)

        if self._create_raises is not None:
            raise self._create_raises

        if self._create_returns is None:
            raise AssertionError("create() was called but no outcome was configured")

        return self._create_returns

    async def get_by_id(self, repository_id: UUID) -> Repository | None:
        """Record the id and return the configured entity, or `None` for a miss.

        Args:
            repository_id: The id the use case looked up.

        Returns:
            The configured entity, or `None`.
        """
        self.get_by_id_calls.append(repository_id)
        return self._get_by_id_returns

    async def get_by_name(self, name: str) -> Repository | None:
        """Record the name and return the configured entity, or `None` for a miss.

        Args:
            name: The name the use case looked up, verbatim.

        Returns:
            The configured entity, or `None`.
        """
        self.get_by_name_calls.append(name)
        return self._get_by_name_returns

    async def list(self, *, limit: int, offset: int) -> RepositoryPage:
        """Record the bounds the use case applied and echo them back on the page.

        Args:
            limit: The page size as received — post-clamp, since clamping happens
                above this call.
            offset: The offset as received, likewise post-clamp.

        Returns:
            A page carrying the configured items and total, with `limit` and
            `offset` set to the values this call was made with.
        """
        self.list_calls.append(PageRequest(limit=limit, offset=offset))

        page = RepositoryPage(
            items=self._page_items[:],
            total=self._page_total,
            limit=limit,
            offset=offset,
        )
        self.list_results.append(page)

        return page

    async def delete(self, repository_id: UUID) -> bool:
        """Record the id, then report whether a repository with it exists.

        Args:
            repository_id: The id the use case asked to delete.

        Returns:
            The configured `delete_returns`.

        Raises:
            AssertionError: If no outcome was configured, meaning the use case
                reached persistence unexpectedly.
        """
        self.delete_calls.append(repository_id)

        if self._delete_returns is None:
            raise AssertionError("delete() was called but no outcome was configured")

        return self._delete_returns


def _assert_protocol_conformance(store: FakeRepositoryStore) -> RepositoryStore:
    """Make the double's conformance to the port a mypy-checked fact.

    Args:
        store: The double to check.

    Returns:
        The same object, seen as the port.
    """
    return store
