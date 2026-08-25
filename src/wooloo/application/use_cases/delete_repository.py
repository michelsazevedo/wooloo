"""
The delete-repository use case.

"""

from uuid import UUID

from wooloo.domain.repositories.contracts import RepositoryStore
from wooloo.domain.repositories.exceptions import RepositoryNotFound
from wooloo.infrastructure.logging.logger import logger


class DeleteRepositoryUseCase:
    """
    Soft-deletes a repository idempotently, distinguishing "gone" from "never was".

    """

    def __init__(self, store: RepositoryStore) -> None:
        """Initialize the use case.

        Args:
            store: The persistence port used to delete the repository. Typically
                a request-scoped `SqlAlchemyRepositoryStore`, but any object
                satisfying the protocol will do.
        """
        self._store = store

    async def execute(self, repository_id: UUID) -> None:
        """Soft-delete a repository, or fail if the id was never registered.

        Args:
            repository_id: The database-assigned identifier of the repository to
                delete.

        Returns:
            `None`. A successful delete has nothing to report: the repository is
            gone, and returning the deleted entity would hand the caller a row it
            can no longer act on.

        Raises:
            RepositoryNotFound: If no repository has ever had this id. Note that a
                repository that exists but was already deleted does *not* raise —
                that is the idempotent success case described above.
        """
        existed = await self._store.delete(repository_id)

        if not existed:
            raise RepositoryNotFound(f"repository not found: id={repository_id}")

        logger.info("repository_deleted", repository_id=str(repository_id))
