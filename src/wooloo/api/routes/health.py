"""
HTTP routes for health probing.

"""

from typing import Annotated

from fastapi import APIRouter, Depends

from wooloo.application.services.health_service import HealthService
from wooloo.infrastructure.database.engine import SessionDep
from wooloo.infrastructure.logging.logger import logger

router = APIRouter(tags=["health"])


def get_health_service(session: SessionDep) -> HealthService:
    """Build a :class:`HealthService` bound to the request-scoped session.

    Args:
        session: Request-scoped session supplied by :func:`get_db_session`.

    Returns:
        A service bound to that session.
    """
    return HealthService(session)


HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]


@router.get("/healthz", summary="Report application and database health")
async def healthz(health_service: HealthServiceDep) -> dict[str, str]:
    """Report process liveness and PostgreSQL connectivity.

    Returns:
        ``{"status": "ok", "database": "up"}`` when the database is reachable,
        otherwise ``{"status": "degraded", "database": "down"}``.
    """
    logger.info("health_check_requested")

    result = await health_service.get_status()

    if result["database"] == "down":
        logger.warning("database_unavailable")

    return result
