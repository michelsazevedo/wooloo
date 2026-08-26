"""
FastAPI application assembly.

"""

from typing import Any

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import Span as SDKSpan
from opentelemetry.trace import Span
from starlette.types import ASGIApp

from wooloo.api.errors.exceptions import WoolooException
from wooloo.api.errors.handlers import (
    blob_already_exists_handler,
    blob_not_found_handler,
    invalid_repository_name_handler,
    repository_already_exists_handler,
    repository_not_found_handler,
    unhandled_exception_handler,
    wooloo_exception_handler,
)
from wooloo.api.middleware.max_body_size import MaxBodySizeMiddleware
from wooloo.api.middleware.request_logging import RequestLoggingMiddleware
from wooloo.api.routes.health import router as health_router
from wooloo.api.routes.repositories import router as repositories_router
from wooloo.api.routes.storage import router as storage_router
from wooloo.application.lifecycle import lifespan
from wooloo.config.settings import get_settings
from wooloo.domain.repositories.exceptions import (
    InvalidRepositoryName,
    RepositoryAlreadyExists,
    RepositoryNotFound,
)
from wooloo.domain.storage.exceptions import BlobAlreadyExists, BlobNotFound

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

app.add_exception_handler(WoolooException, wooloo_exception_handler)  # type: ignore[arg-type]
app.add_exception_handler(InvalidRepositoryName, invalid_repository_name_handler)  # type: ignore[arg-type]
app.add_exception_handler(RepositoryAlreadyExists, repository_already_exists_handler)  # type: ignore[arg-type]
app.add_exception_handler(RepositoryNotFound, repository_not_found_handler)  # type: ignore[arg-type]
app.add_exception_handler(BlobNotFound, blob_not_found_handler)  # type: ignore[arg-type]
app.add_exception_handler(BlobAlreadyExists, blob_already_exists_handler)  # type: ignore[arg-type]
app.add_exception_handler(Exception, unhandled_exception_handler)

app.include_router(health_router, prefix="/api/v1")
app.include_router(repositories_router, prefix="/api/v1/repositories")
app.include_router(storage_router, prefix="/api/v1/storage")

asgi_app: ASGIApp = RequestLoggingMiddleware(
    MaxBodySizeMiddleware(app, get_settings().max_upload_bytes)
)
