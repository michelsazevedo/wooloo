"""
Application-layer health checks.

"""

import asyncio
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from wooloo.domain.storage.contracts import BlobStorage

_CONNECTIVITY_PROBE: Final = text("SELECT 1")

_PROBE_TIMEOUT_SECONDS: Final = 2.0
"""
Deadline every dependency probe is bounded by.

"""

_STATUS_OK: Final = "ok"
_STATUS_DEGRADED: Final = "degraded"

_UP: Final = "up"
_DOWN: Final = "down"


class HealthService:
    """
    Reports application health, including PostgreSQL and blob storage.

    """

    def __init__(self, session: AsyncSession, storage: BlobStorage) -> None:
        """Initialize the service.

        Args:
            session: Request-scoped async SQLAlchemy session used for the probe.
            storage: The blob storage port. Only its readiness probe is used; what
                that probe actually does is the backend's business.
        """
        self._session = session
        self._storage = storage

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

    async def check_storage(self) -> bool:
        """Probe blob storage readiness under the same deadline as the database.

        Returns:
            `True` if the configured backend reports itself ready, `False` if it
            reports itself unready or fails to answer within
            :data:`_PROBE_TIMEOUT_SECONDS`.
        """
        try:
            async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
                return await self._storage.check_health()
        except TimeoutError:
            return False

    async def get_status(self) -> dict[str, str]:
        """Build the health payload for the current dependency states.

        Returns:
            `{"status": ..., "database": ..., "storage": ...}`, where each
            dependency is `"up"` or `"down"` and `status` is `"ok"` only when both
            are up.
        """
        database_ok = await self.check_database()
        storage_ok = await self.check_storage()

        return {
            "status": _STATUS_OK if database_ok and storage_ok else _STATUS_DEGRADED,
            "database": _UP if database_ok else _DOWN,
            "storage": _UP if storage_ok else _DOWN,
        }
