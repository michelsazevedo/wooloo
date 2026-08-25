"""
HTTP routes for repository management.

"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response

from wooloo.api.schemas.repository import (
    CreateRepositoryRequest,
    RepositoryListResponse,
    RepositoryResponse,
)
from wooloo.application.use_cases.create_repository import CreateRepositoryUseCase
from wooloo.application.use_cases.delete_repository import DeleteRepositoryUseCase
from wooloo.application.use_cases.get_repository import GetRepositoryUseCase
from wooloo.application.use_cases.list_repositories import ListRepositoriesUseCase
from wooloo.infrastructure.database.engine import SessionDep
from wooloo.infrastructure.repositories.store import SqlAlchemyRepositoryStore

router = APIRouter(tags=["repositories"])


def get_repository_store(session: SessionDep) -> SqlAlchemyRepositoryStore:
    """Build a store bound to the request-scoped session.

    Args:
        session: Request-scoped session supplied by :func:`get_db_session`.

    Returns:
        A store bound to that session.
    """
    return SqlAlchemyRepositoryStore(session)


RepositoryStoreDep = Annotated[SqlAlchemyRepositoryStore, Depends(get_repository_store)]


def get_create_repository_use_case(store: RepositoryStoreDep) -> CreateRepositoryUseCase:
    """Build the create-repository use case for this request.

    Args:
        store: Request-scoped store supplied by :func:`get_repository_store`. It is
            accepted as the concrete adapter and passed where a `RepositoryStore`
            protocol is expected, so mypy checks conformance at this line.

    Returns:
        A use case bound to that store.
    """
    return CreateRepositoryUseCase(store)


CreateRepositoryUseCaseDep = Annotated[
    CreateRepositoryUseCase, Depends(get_create_repository_use_case)
]


def get_get_repository_use_case(store: RepositoryStoreDep) -> GetRepositoryUseCase:
    """Build the get-repository use case for this request.

    Args:
        store: Request-scoped store supplied by :func:`get_repository_store`.

    Returns:
        A use case bound to that store.
    """
    return GetRepositoryUseCase(store)


GetRepositoryUseCaseDep = Annotated[GetRepositoryUseCase, Depends(get_get_repository_use_case)]


def get_list_repositories_use_case(store: RepositoryStoreDep) -> ListRepositoriesUseCase:
    """Build the list-repositories use case for this request.

    Args:
        store: Request-scoped store supplied by :func:`get_repository_store`.

    Returns:
        A use case bound to that store.
    """
    return ListRepositoriesUseCase(store)


ListRepositoriesUseCaseDep = Annotated[
    ListRepositoriesUseCase, Depends(get_list_repositories_use_case)
]


def get_delete_repository_use_case(store: RepositoryStoreDep) -> DeleteRepositoryUseCase:
    """Build the delete-repository use case for this request.

    Args:
        store: Request-scoped store supplied by :func:`get_repository_store`.

    Returns:
        A use case bound to that store.
    """
    return DeleteRepositoryUseCase(store)


DeleteRepositoryUseCaseDep = Annotated[
    DeleteRepositoryUseCase, Depends(get_delete_repository_use_case)
]


@router.post("", status_code=201, summary="Register a repository")
async def create_repository(
    request: CreateRepositoryRequest,
    use_case: CreateRepositoryUseCaseDep,
) -> RepositoryResponse:
    """Register a new repository under the requested name.

    Args:
        request: The parsed request body, carrying the desired name. It is passed
            through unexamined — whether the name satisfies the OCI grammar is a
            domain question answered by `RepositoryName`, not a shape question
            this layer could answer without duplicating that rule.
        use_case: The injected create-repository use case.

    Returns:
        The persisted repository, under `201 Created`.

    Raises:
        InvalidRepositoryName: If the name violates the OCI grammar. Left to
            propagate to the registered handler, which answers `400`.
        RepositoryAlreadyExists: If the name is already registered. Answered `409`
            by its handler.
    """
    repository = await use_case.execute(request.name)

    return RepositoryResponse.model_validate(repository)


@router.get("", summary="List repositories")
async def list_repositories(
    use_case: ListRepositoriesUseCaseDep,
    limit: int = 20,
    offset: int = 0,
) -> RepositoryListResponse:
    """Return one page of repositories, newest first.

    Args:
        use_case: The injected list-repositories use case.
        limit: Maximum repositories to return. The default matches the use case's
            own, so an omitted parameter and an explicit `?limit=20` behave
            identically.
        offset: Repositories to skip before the page starts.

    Returns:
        The page's repositories alongside the total count of non-deleted
        repositories and the `limit`/`offset` that produced them.
    """
    page = await use_case.execute(limit=limit, offset=offset)

    return RepositoryListResponse(
        items=[RepositoryResponse.model_validate(repository) for repository in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{repository_id}", summary="Retrieve a repository")
async def get_repository(repository_id: UUID, use_case: GetRepositoryUseCaseDep) -> RepositoryResponse:
    """Return the repository with this id.

    No `status_code` is declared: `200` is FastAPI's default for a normal return,
    and `healthz` likewise states only the codes that differ from it.

    Args:
        repository_id: The repository's database-assigned identifier. A value that
            is not a UUID is rejected by FastAPI's own path parsing before this
            function runs.
        use_case: The injected get-repository use case.

    Returns:
        The matching repository.

    Raises:
        RepositoryNotFound: If no active repository has this id, whether it was
            soft-deleted or never created. Answered `404` by its handler.
    """
    repository = await use_case.by_id(repository_id)

    return RepositoryResponse.model_validate(repository)


@router.delete("/{repository_id}", status_code=204, response_class=Response, summary="Delete a repository")
async def delete_repository(
    repository_id: UUID,
    use_case: DeleteRepositoryUseCaseDep,
) -> None:
    """Soft-delete the repository with this id.

    Args:
        repository_id: The repository's database-assigned identifier.
        use_case: The injected delete-repository use case.

    Returns:
        `None`, serialised as an empty `204 No Content` body — there is nothing
        useful to hand back about a repository that no longer exists.

    Raises:
        RepositoryNotFound: If no repository has ever had this id. Answered `404`
            by its handler.
    """
    await use_case.execute(repository_id)
