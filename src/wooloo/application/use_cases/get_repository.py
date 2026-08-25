"""
Retrieval orchestration for a single repository, by id or by name.

"""

from uuid import UUID

from wooloo.domain.repositories.contracts import RepositoryStore
from wooloo.domain.repositories.entity import Repository
from wooloo.domain.repositories.exceptions import RepositoryNotFound
from wooloo.infrastructure.logging.logger import logger


def _log_retrieved(repository: Repository) -> None:
    """Emit the structured event recording a successful single-repository lookup.

    Args:
        repository: The repository that was found. Logged by id and name rather
            than whole, so the record stays a stable, low-cardinality shape
            instead of tracking the entity's fields.
    """
    logger.info(
        "repository_retrieved",
        repository_id=str(repository.id),
        repository_name=repository.name,
    )


class GetRepositoryUseCase:
    """
    Fetches a single repository, raising when there is nothing to return.

    """

    def __init__(self, store: RepositoryStore) -> None:
        """Initialize the use case.

        Args:
            store: The persistence port used for both lookups. Any object
                structurally satisfying `RepositoryStore` is accepted —
                conformance is checked by mypy, not by inheritance.
        """
        self._store = store

    async def by_id(self, repository_id: UUID) -> Repository:
        """Fetch the active repository with this id.

        Args:
            repository_id: The repository's database-assigned identifier.

        Returns:
            The matching repository. Never `None` — an absent repository is
            reported by the exception below, so callers get an entity or an
            error and never a value they must re-check.

        Raises:
            RepositoryNotFound: If no active repository has this id, whether it
                was soft-deleted or never created. The message carries the id
                that missed, so a log line or 404 body identifies what was
                actually looked up.
        """
        repository = await self._store.get_by_id(repository_id)

        if repository is None:
            raise RepositoryNotFound(f"repository not found: id={repository_id}")

        _log_retrieved(repository)
        return repository

    async def by_name(self, name: str) -> Repository:
        """Fetch the active repository with this name.

        Args:
            name: The repository name to look up, for example `library/nginx`.
                Matching is case-insensitive, delegated to the store, which
                compares names the same way the unique constraint on creation
                does.

        Returns:
            The matching repository, on the same terms as `by_id`.

        Raises:
            RepositoryNotFound: If no active repository has this name. The name
                is repr-quoted in the message so a value that is empty or
                surrounded by whitespace is still visible in the output.
        """
        repository = await self._store.get_by_name(name)

        if repository is None:
            raise RepositoryNotFound(f"repository not found: name={name!r}")

        _log_retrieved(repository)
        return repository
