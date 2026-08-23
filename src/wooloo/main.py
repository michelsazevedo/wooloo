"""
FastAPI application assembly.

Run with ``uv run uvicorn wooloo.main:app --reload``.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from wooloo.api.routes.health import router as health_router
from wooloo.config.settings import get_settings
from wooloo.infrastructure.database.engine import dispose_engine, get_engine


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Validate configuration at boot and release database resources at shutdown.

    Args:
        _app: The application being started. Unused — state is held by the cached
            providers in their own modules rather than on ``app.state``.

    Yields:
        Control to the running application, once configuration is known good.

    Raises:
        pydantic.ValidationError: If ``DATABASE_URL`` is absent from both the
            environment and the ``.env`` file.
        sqlalchemy.exc.ArgumentError: If ``DATABASE_URL`` is present but not a
            parseable SQLAlchemy DSN.
    """
    get_settings()
    get_engine()
    try:
        yield
    finally:
        await dispose_engine()


app = FastAPI(lifespan=lifespan)

app.include_router(health_router, prefix="/api/v1")
