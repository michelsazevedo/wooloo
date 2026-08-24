"""
Application-layer health checks.

"""

import asyncio
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_CONNECTIVITY_PROBE: Final = text("SELECT 1")

_PROBE_TIMEOUT_SECONDS: Final = 2.0

_STATUS_HEALTHY: Final[dict[str, str]] = {"status": "ok", "database": "up"}
_STATUS_DEGRADED: Final[dict[str, str]] = {"status": "degraded", "database": "down"}


class HealthService:
    """Reports application health, including PostgreSQL connectivity.

    The `AsyncSession` is injected so this service has no knowledge of how the
    session is created or scoped, which keeps it trivially unit-testable with a
    mocked session.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Initialize the service.

        Args:
            session: Request-scoped async SQLAlchemy session used for the probe.
        """
        self._session = session

    async def check_database(self) -> bool:
        """Probe database connectivity by issuing `SELECT 1` under a timeout.

        Returns:
            `True` if the query succeeded, `False` if it failed or timed out.
        """
        try:
            async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
                await self._session.execute(_CONNECTIVITY_PROBE)
        except Exception:
            return False
        return True

    async def get_status(self) -> dict[str, str]:
        """Build the health payload for the current database state.

        Returns:
            `{"status": "ok", "database": "up"}` when the database is
            reachable, otherwise `{"status": "degraded", "database": "down"}`.
        """
        if await self.check_database():
            return dict(_STATUS_HEALTHY)
        return dict(_STATUS_DEGRADED)
