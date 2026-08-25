"""
The SQLAlchemy adapter satisfying the `RepositoryStore` port.

"""

from typing import Final
from uuid import UUID

from sqlalchemy import ColumnExpressionArgument, func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from wooloo.domain.repositories.contracts import RepositoryPage
from wooloo.domain.repositories.entity import Repository
from wooloo.domain.repositories.exceptions import RepositoryAlreadyExists
from wooloo.infrastructure.database.models.repository import RepositoryModel

_UNIQUE_VIOLATION_SQLSTATE: Final = "23505"

_NAME_UNIQUE_INDEX: Final = "ix_repositories_name"


def _is_duplicate_name_violation(error: IntegrityError) -> bool:
    """Decide whether an integrity error is a collision on `repositories.name`.

    Args:
        error: The integrity error raised by the failing flush.

    Returns:
        `True` if the error is a unique violation attributable to the name index,
        `False` for any other integrity failure.
    """
    driver_error = error.orig
    if driver_error is None:
        return False

    sqlstate: object = getattr(driver_error, "sqlstate", None)
    if sqlstate != _UNIQUE_VIOLATION_SQLSTATE:
        return False

    constraint_name: object = getattr(driver_error.__cause__, "constraint_name", None)
    return constraint_name is None or constraint_name == _NAME_UNIQUE_INDEX


class SqlAlchemyRepositoryStore:
    """
    Persists repositories in PostgreSQL through an injected async session.

    """

    def __init__(self, session: AsyncSession) -> None:
        """Initialize the store.

        Args:
            session: Request-scoped async SQLAlchemy session used for every
                statement this store issues.
        """
        self._session = session

    async def create(self, name: str) -> Repository:
        """Insert a repository and return it with its database-generated fields.

        Args:
            name: The already-validated repository name to register.

        Returns:
            The persisted repository.

        Raises:
            RepositoryAlreadyExists: If the name is already taken, including by a
                soft-deleted repository, which still occupies its name. Raised
                without a message: the presentation layer's generic wording says
                only that *something* conflicts, so an unauthenticated caller
                cannot use a `409` to confirm that one specific name is
                registered — including names whose rows are soft-deleted and
                therefore invisible on every other route. Scoping who may see a
                conflict at all is an authorization concern this store cannot
                settle; withholding the name is the part it can.
            sqlalchemy.exc.DBAPIError: If the insert failed for any other database
                reason. Re-raised unchanged rather than mislabelled as a name
                conflict, so it surfaces as an honest `500`.
        """
        model = RepositoryModel(name=name)
        self._session.add(model)

        try:
            await self._session.flush()
        except DBAPIError as exc:
            await self._session.rollback()
            if isinstance(exc, IntegrityError) and _is_duplicate_name_violation(exc):
                raise RepositoryAlreadyExists from exc
            raise

        await self._session.commit()
        return self._to_entity(model)

    async def get_by_id(self, repository_id: UUID) -> Repository | None:
        """Fetch the active repository with this id.

        Args:
            repository_id: The repository's database-assigned identifier.

        Returns:
            The matching repository, or `None` if no active repository has this
            id — a soft-deleted row counts as absent.
        """
        return await self._find_active(RepositoryModel.id == repository_id)

    async def get_by_name(self, name: str) -> Repository | None:
        """Fetch the active repository with this name.

        Args:
            name: The repository name to look up.

        Returns:
            The matching repository, or `None` if no active repository has this
            name — a soft-deleted row counts as absent.
        """
        return await self._find_active(RepositoryModel.name == name)

    async def list(self, *, limit: int, offset: int) -> RepositoryPage:
        """Return one page of active repositories, newest created first.

        Args:
            limit: Maximum number of repositories to return.
            offset: Number of repositories to skip before the page starts.

        Returns:
            The page's items alongside the total count of active repositories and
            the `limit`/`offset` that produced it.
        """
        active = RepositoryModel.deleted_at.is_(None)

        page_statement = (
            select(RepositoryModel)
            .where(active)
            .order_by(RepositoryModel.created_at.desc(), RepositoryModel.id.desc())
            .limit(limit)
            .offset(offset)
        )
        total_statement = select(func.count()).select_from(RepositoryModel).where(active)

        page_result = await self._session.execute(page_statement)
        total: int = (await self._session.execute(total_statement)).scalar_one()

        return RepositoryPage(
            items=[self._to_entity(model) for model in page_result.scalars()],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def delete(self, repository_id: UUID) -> bool:
        """Soft-delete a repository and report whether the id exists at all.

        Args:
            repository_id: The repository's database-assigned identifier.

        Returns:
            `True` if a repository with this id exists, whether this call deleted
            it or an earlier one did. `False` if no such repository has ever
            existed.
        """
        statement = (
            update(RepositoryModel)
            .where(RepositoryModel.id == repository_id)
            .values(deleted_at=func.coalesce(RepositoryModel.deleted_at, func.now()))
            .returning(RepositoryModel.id)
        )

        result = await self._session.execute(statement)
        deleted_id = result.scalar_one_or_none()
        await self._session.commit()

        return deleted_id is not None

    async def _find_active(
        self, criterion: ColumnExpressionArgument[bool]
    ) -> Repository | None:
        """Fetch the one non-deleted row matching a criterion, if it exists.

        Args:
            criterion: The column predicate identifying the row, combined with the
                soft-deletion filter.

        Returns:
            The matching repository, or `None` if nothing active matched.
        """
        statement = select(RepositoryModel).where(
            criterion,
            RepositoryModel.deleted_at.is_(None),
        )
        model = (await self._session.execute(statement)).scalar_one_or_none()

        return None if model is None else self._to_entity(model)

    def _to_entity(self, model: RepositoryModel) -> Repository:
        """Translate a persisted row into its domain entity.

        Args:
            model: A row whose database-generated columns are already populated,
                which holds after a flush or a load but not for a pending object.

        Returns:
            The equivalent `Repository`.
        """
        return Repository(
            id=model.id,
            name=model.name,
            created_at=model.created_at,
            updated_at=model.updated_at,
            deleted_at=model.deleted_at,
        )
