"""
Request and response bodies for the repository endpoints.

"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CreateRepositoryRequest(BaseModel):
    """Body of ``POST /api/v1/repositories``.

    Attributes:
        name: The repository name to create, e.g. ``acme/backend-api``.
            Validated against the OCI grammar further down the stack, which
            rejects it with ``InvalidRepositoryName`` (surfaced as ``400``).
    """

    name: str


class RepositoryResponse(BaseModel):
    """A single repository as returned to an API consumer.

    Attributes:
        id: Database-generated identifier of the repository.
        name: The repository's OCI name as stored, e.g. ``acme/backend-api``.
        created_at: Timezone-aware creation timestamp.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID

    name: str

    created_at: datetime


class RepositoryListResponse(BaseModel):
    """One page of repositories, plus the metadata needed to page through them.

    Attributes:
        items: The repositories on this page, newest first.
        total: Count of all non-deleted repositories matching the query.
        limit: Maximum number of items the server used for this page.
        offset: Number of items skipped before this page.
    """

    items: list[RepositoryResponse]

    total: int

    limit: int

    offset: int
