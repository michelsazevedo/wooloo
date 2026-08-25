"""
FastAPI application assembly.

Run with ``uv run uvicorn wooloo.main:asgi_app --reload`` — ``asgi_app``, not
``app``: the served object is the FastAPI application wrapped in
``RequestLoggingMiddleware``, and serving ``app`` directly would silently drop
request correlation from every response.

"""

from typing import Any

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import Span as SDKSpan
from opentelemetry.trace import Span
from starlette.types import ASGIApp

from wooloo.api.errors.exceptions import WoolooException
from wooloo.api.errors.handlers import (
    invalid_repository_name_handler,
    repository_already_exists_handler,
    repository_not_found_handler,
    unhandled_exception_handler,
    wooloo_exception_handler,
)
from wooloo.api.middleware.request_logging import RequestLoggingMiddleware
from wooloo.api.routes.health import router as health_router
from wooloo.api.routes.repositories import router as repositories_router
from wooloo.application.lifecycle import lifespan
from wooloo.domain.repositories.exceptions import (
    InvalidRepositoryName,
    RepositoryAlreadyExists,
    RepositoryNotFound,
)

app = FastAPI(title="Wooloo", lifespan=lifespan)


def _strip_query_string(span: Span, _scope: dict[str, Any]) -> None:
    """Remove the query string from a server span's ``http.url`` attribute.

    Args:
        span: The server span the instrumentor has just created. Typed as the
            API-level :class:`~opentelemetry.trace.Span` because that is what the
            hook contract passes; narrowed below to reach ``attributes``, which
            only the SDK implementation exposes.
        _scope: The ASGI connection scope. Unused — the URL is read back off the
            span rather than rebuilt from the scope, so this cannot disagree with
            whatever the instrumentor actually recorded.
    """
    if not isinstance(span, SDKSpan) or not span.is_recording():
        return

    url = (span.attributes or {}).get("http.url")

    if isinstance(url, str):
        span.set_attribute("http.url", url.split("?", 1)[0])


FastAPIInstrumentor.instrument_app(
    app,
    server_request_hook=_strip_query_string,
    exclude_spans=["send", "receive"],
)

# Every `# type: ignore[arg-type]` below is the same one cause. Starlette types a
# handler as accepting `Exception`, so a handler narrowed to the exception it
# actually handles is, to the type checker, an unsafe contravariant substitution.
# Widening the parameters to `Exception` would silence it by making each handler
# accept failures it cannot render and forcing an `isinstance` check back into the
# body. The registration is what guarantees the narrow type at runtime, so the
# ignore is confined to these lines and the handlers stay honestly typed.
app.add_exception_handler(WoolooException, wooloo_exception_handler)  # type: ignore[arg-type]
app.add_exception_handler(InvalidRepositoryName, invalid_repository_name_handler)  # type: ignore[arg-type]
app.add_exception_handler(RepositoryAlreadyExists, repository_already_exists_handler)  # type: ignore[arg-type]
app.add_exception_handler(RepositoryNotFound, repository_not_found_handler)  # type: ignore[arg-type]
app.add_exception_handler(Exception, unhandled_exception_handler)

app.include_router(health_router, prefix="/api/v1")
app.include_router(repositories_router, prefix="/api/v1/repositories")

asgi_app: ASGIApp = RequestLoggingMiddleware(app)
