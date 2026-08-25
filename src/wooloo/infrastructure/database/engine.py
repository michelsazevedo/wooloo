"""
Async SQLAlchemy engine, session factory, and FastAPI session dependency.

"""

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from wooloo.config.settings import get_settings

_POOL_SIZE = 5

_MAX_OVERFLOW = 10

_POOL_RECYCLE_SECONDS = 1800

_POOL_TIMEOUT_SECONDS = 5.0

_CONNECT_TIMEOUT_SECONDS = 3.0
_COMMAND_TIMEOUT_SECONDS = 3.0


@lru_cache
def get_engine() -> AsyncEngine:
    """Return the process-wide async engine.

    Returns:
        The shared :class:`AsyncEngine`.

    Raises:
        pydantic.ValidationError: If ``DATABASE_URL`` is absent from both the
            environment and the ``.env`` file.
        sqlalchemy.exc.ArgumentError: If ``DATABASE_URL`` is present but is not a
            parseable SQLAlchemy DSN.
    """
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        hide_parameters=True,
        pool_size=_POOL_SIZE,
        max_overflow=_MAX_OVERFLOW,
        pool_pre_ping=True,
        pool_recycle=_POOL_RECYCLE_SECONDS,
        pool_timeout=_POOL_TIMEOUT_SECONDS,
        connect_args={
            "timeout": _CONNECT_TIMEOUT_SECONDS,
            "command_timeout": _COMMAND_TIMEOUT_SECONDS,
        },
    )


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide :class:`AsyncSession` factory.

    ``expire_on_commit`` is disabled so attributes loaded before a commit stay
    readable afterwards; with an async session, re-fetching an expired attribute
    would otherwise trigger implicit lazy I/O outside an ``await``.

    Returns:
        A factory producing sessions bound to :func:`get_engine`.
    """
    return async_sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,
    )


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped :class:`AsyncSession`, closing it afterwards.

    Intended for consumption through :data:`SessionDep` rather than referenced
    directly by routes.

    Yields:
        A session bound to the shared engine.
    """
    async with get_session_factory()() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_db_session)]
"""
Request-scoped session, injected by FastAPI.

"""


async def dispose_engine() -> None:
    """
    Close all pooled connections held by the engine and drop the cached providers.

    """
    if get_engine.cache_info().currsize == 0:
        return
    await get_engine().dispose()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
