"""
The `RepositoryStore` persistence port and the `RepositoryPage` result it returns.

"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from wooloo.domain.repositories.entity import Repository


@dataclass(frozen=True)
class RepositoryPage:
    """One page of repositories, plus the counters needed to paginate through them.

    Attributes:
        items: The repositories on this page, ordered newest-created first. Empty
            when `offset` is past the end of the collection — an empty page is a
            normal result, never an error.
        total: How many non-deleted repositories exist in total, independent of
            `limit` and `offset`.
        limit: The maximum page size that produced `items`, echoed back so the
            caller can render pagination controls without re-deriving it from the
            request.
        offset: The number of repositories skipped before this page, echoed back
            for the same reason.
    """

    items: list[Repository]

    total: int

    limit: int

    offset: int


class RepositoryStore(Protocol):
    """
    The persistence contract for repositories.

    """

    async def create(self, name: str) -> Repository:
        """Insert a new repository and return it fully populated.

        Args:
            name: The already-validated repository name to register, for example
                `acme/backend-api`.

        Returns:
            The persisted repository, with the database-assigned `id` and
            timestamps filled in and `deleted_at` set to `None`.

        Raises:
            RepositoryAlreadyExists: If a repository with this name is already
                registered. Names are compared case-insensitively, so a name
                differing only in case is a conflict, not a new repository. A
                soft-deleted repository still occupies its name, so re-creating one
                also conflicts.
        """
        ...

    async def get_by_id(self, repository_id: UUID) -> Repository | None:
        """Fetch the active repository with this id, if there is one.

        Args:
            repository_id: The repository's database-assigned identifier.

        Returns:
            The matching repository, or `None` if no active repository has this id.

        Raises:
            Nothing contractual. In particular this method never raises
            `RepositoryNotFound`.
        """
        ...

    async def get_by_name(self, name: str) -> Repository | None:
        """Fetch the active repository with this name, if there is one.

        Args:
            name: The repository name to look up, for example `library/nginx`.
                Matching is case-insensitive, mirroring how the unique constraint
                on `create` behaves, so lookup and creation agree on what counts as
                the same name.

        Returns:
            The matching repository, or `None` if no active repository has this
            name.

        Raises:
            Nothing contractual. In particular this method never raises
            `RepositoryNotFound`.
        """
        ...

    async def list(self, *, limit: int, offset: int) -> RepositoryPage:
        """Return one page of active repositories, newest first.

        Args:
            limit: Maximum number of repositories to return.
            offset: Number of repositories to skip before the page starts.

        Returns:
            A `RepositoryPage` carrying the page's items alongside the total count
            of active repositories and the `limit`/`offset` that produced it.

        Raises:
            Nothing contractual.
        """
        ...

    async def delete(self, repository_id: UUID) -> bool:
        """Soft-delete a repository, idempotently, and report whether the id exists.

        Args:
            repository_id: The repository's database-assigned identifier.

        Returns:
            `True` if a repository with this id exists, whether it was deleted by
            this call or was already deleted. `False` if no such repository has ever
            existed.

        Raises:
            Nothing contractual. In particular a repeat delete is a success, not a
            `RepositoryNotFound`, and an unknown id is reported by the return value
            rather than by an exception.
        """
        ...
