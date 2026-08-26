"""
HTTP routes for health probing.

"""

from typing import Annotated

from fastapi import APIRouter, Depends

from wooloo.application.services.health_service import HealthService
from wooloo.infrastructure.database.engine import SessionDep
from wooloo.infrastructure.logging.logger import logger
from wooloo.infrastructure.storage.deps import BlobStorageDep

router = APIRouter(tags=["health"])


def get_health_service(session: SessionDep, storage: BlobStorageDep) -> HealthService:
    """Build a :class:`HealthService` bound to this request's dependencies.

    Args:
        session: Request-scoped session supplied by :func:`get_db_session`.
        storage: The configured blob storage adapter supplied by
            :func:`get_blob_storage`.

    Returns:
        A service bound to that session and adapter.
    """
    return HealthService(session, storage)


HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]


@router.get("/healthz", summary="Report application, database, and storage health")
async def healthz(health_service: HealthServiceDep) -> dict[str, str]:
    """Report process liveness, PostgreSQL connectivity, and storage readiness.

    Each dependency gets its own warning rather than one shared "something is
    down": the two failures have different causes and different fixes, and an
    operator grepping for either needs to find it by name.

    Returns:
        ``{"status": "ok", "database": "up", "storage": "up"}`` when both
        dependencies are reachable; otherwise ``status`` is ``"degraded"`` and the
        failing dependency's own field reads ``"down"``. The HTTP status is
        ``200`` either way — the body carries the verdict.
    """
    logger.info("health_check_requested")

    result = await health_service.get_status()

    if result["database"] == "down":
        logger.warning("database_unavailable")

    if result["storage"] == "down":
        logger.warning("storage_unavailable")

    return result
