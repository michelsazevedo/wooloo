"""
HTTP routes for health probing.

"""

from typing import Annotated

from fastapi import APIRouter, Depends

from wooloo.application.services.health_service import HealthService
from wooloo.infrastructure.database.engine import SessionDep

router = APIRouter(tags=["health"])


def get_health_service(session: SessionDep) -> HealthService:
    """Build a :class:`HealthService` bound to the request-scoped session.

    This is the seam between the API and application layers: FastAPI resolves the
    session, and this provider performs the construction that ``HealthService``
    itself must stay ignorant of. Overriding :func:`get_db_session` in tests
    therefore swaps the service's database without touching the route.

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

    Serves as a combined Kubernetes liveness and readiness probe. The response is
    always HTTP 200 — reaching this handler proves the process is alive, and the
    database verdict is carried in the body rather than the status code. Probes
    must therefore read the ``status`` field, not rely on the status code alone.

    Returns:
        ``{"status": "ok", "database": "up"}`` when the database is reachable,
        otherwise ``{"status": "degraded", "database": "down"}``.
    """
    return await health_service.get_status()
