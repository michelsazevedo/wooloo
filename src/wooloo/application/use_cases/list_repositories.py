"""
Paginated listing of repositories, with page-size bounds applied before the store.

"""

from typing import Final

from wooloo.domain.repositories.contracts import RepositoryPage, RepositoryStore

_MIN_LIMIT: Final = 1

_MAX_LIMIT: Final = 100

_MIN_OFFSET: Final = 0

_DEFAULT_LIMIT: Final = 20

_DEFAULT_OFFSET: Final = 0


def _clamp_limit(limit: int) -> int:
    """Coerce a requested page size into the supported range.

    Args:
        limit: The page size the caller asked for, unvalidated.

    Returns:
        `limit` unchanged when it already falls within `[1, 100]`, otherwise the
        nearest bound: values below 1 become 1, values above 100 become 100.
    """
    return min(max(limit, _MIN_LIMIT), _MAX_LIMIT)


def _clamp_offset(offset: int) -> int:
    """Coerce a requested offset to a non-negative value.

    Args:
        offset: The number of repositories the caller asked to skip, unvalidated.

    Returns:
        `offset` unchanged when it is non-negative, otherwise 0.
    """
    return max(offset, _MIN_OFFSET)


class ListRepositoriesUseCase:
    """
    Lists repositories one bounded page at a time.

    """

    def __init__(self, store: RepositoryStore) -> None:
        """Initialize the use case.

        Args:
            store: The persistence port used to fetch pages of repositories.
        """
        self._store = store

    async def execute(
        self,
        *,
        limit: int = _DEFAULT_LIMIT,
        offset: int = _DEFAULT_OFFSET,
    ) -> RepositoryPage:
        """Fetch one page of active repositories, newest first.

        Args:
            limit: Maximum repositories to return. Clamped to `[1, 100]` as
                described above. Defaults to 20.
            offset: Repositories to skip before the page starts. Clamped to a
                minimum of 0. Defaults to 0.

        Returns:
            A `RepositoryPage` whose `limit` and `offset` are the clamped values
            actually applied, alongside the page's items and the total count of
            active repositories.

        Raises:
            Nothing contractual. Infrastructure failures raised by the store — a
            lost connection, a statement timeout — propagate unchanged, since this
            use case has no business decision to make about them.
        """
        return await self._store.list(
            limit=_clamp_limit(limit),
            offset=_clamp_offset(offset),
        )
