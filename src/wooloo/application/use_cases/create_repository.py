"""
The create-repository use case.

"""

from wooloo.domain.repositories.contracts import RepositoryStore
from wooloo.domain.repositories.entity import Repository
from wooloo.domain.repositories.name import RepositoryName
from wooloo.infrastructure.logging.logger import logger


class CreateRepositoryUseCase:
    """
    Registers a new repository: validate the name, persist it, record the outcome.

    """

    def __init__(self, store: RepositoryStore) -> None:
        """Initialize the use case.

        Args:
            store: The persistence port used to register the repository. Typically
                a request-scoped `SqlAlchemyRepositoryStore`, but any object
                satisfying the protocol will do.
        """
        self._store = store

    async def execute(self, name: str) -> Repository:
        """Validate a repository name and register it.

        Args:
            name: The caller-supplied repository name, for example
                `acme/backend-api`. Surrounding whitespace is stripped during
                validation; nothing else about the input is rewritten.

        Returns:
            The persisted repository, carrying the database-assigned `id` and
            timestamps.

        Raises:
            InvalidRepositoryName: If the name violates the OCI naming grammar. The
                store is not called in this case, so no row and no round trip
                results.
            RepositoryAlreadyExists: If a repository with this name is already
                registered, raised by the store and propagated unchanged.
        """
        validated_name = RepositoryName(name)

        repository = await self._store.create(str(validated_name))

        logger.info(
            "repository_created",
            repository_id=str(repository.id),
            repository_name=repository.name,
        )

        return repository
