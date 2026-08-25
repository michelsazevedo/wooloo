"""
Presentation of raised exceptions as HTTP error responses.

"""

from dataclasses import dataclass
from typing import Final

from fastapi import Request
from fastapi.responses import JSONResponse

from wooloo.api.errors.exceptions import (
    ConflictException,
    NotFoundException,
    ValidationException,
    WoolooException,
)
from wooloo.api.errors.responses import ErrorResponse
from wooloo.domain.repositories.exceptions import (
    InvalidRepositoryName,
    RepositoryAlreadyExists,
    RepositoryError,
    RepositoryNotFound,
)
from wooloo.infrastructure.logging.logger import logger


@dataclass(frozen=True)
class _ErrorPresentation:
    """How one class of failure is shown to an API consumer.

    Keeping status code, machine-readable code and wording together in one value
    is what lets both handlers share a single response builder: the only thing
    that varies between a 404 and a 500 is which of these is selected.

    Attributes:
        status_code: HTTP status accompanying the body.
        code: Stable machine-readable code clients branch on.
        default_message: Wording used when the exception carries no detail of its
            own. The exception hierarchy deliberately defines no display text, so
            this module owns it — the phrasing shown to clients is a presentation
            concern, not a property of the failure.
    """

    status_code: int

    code: str

    default_message: str


_VALIDATION_ERROR: Final = _ErrorPresentation(
    status_code=400,
    code="validation_error",
    default_message="Validation failed",
)

_NOT_FOUND: Final = _ErrorPresentation(
    status_code=404,
    code="not_found",
    default_message="Resource not found",
)

_CONFLICT: Final = _ErrorPresentation(
    status_code=409,
    code="conflict",
    default_message="Resource already exists",
)

_DOMAIN_PRESENTATIONS: Final[dict[type[WoolooException], _ErrorPresentation]] = {
    ValidationException: _VALIDATION_ERROR,
    NotFoundException: _NOT_FOUND,
    ConflictException: _CONFLICT,
}

_INTERNAL_ERROR: Final = _ErrorPresentation(
    status_code=500,
    code="internal_error",
    default_message="An unexpected error occurred",
)
"""
The answer to a failure nothing anticipated.

"""


async def wooloo_exception_handler(request: Request, exc: WoolooException) -> JSONResponse:
    """Answer a deliberately raised application exception with its mapped status.

    Args:
        request: The request being answered, read only for its correlation ID.
        exc: The raised exception. Its ``message`` is used verbatim when supplied
            — it is written by this codebase for an API consumer, unlike the text
            of an arbitrary unhandled exception.

    Returns:
        An :class:`ErrorResponse` body under the status code mapped to ``exc``'s
        type. A ``WoolooException`` subclass with no mapping is delegated to
        :func:`unhandled_exception_handler`: this layer has no way to guess what
        status an unknown failure deserves, and a guess of ``404`` on a write path
        is worse than an honest ``500``.
    """
    presentation = _resolve_presentation(exc)

    if presentation is None:
        return await unhandled_exception_handler(request, exc)

    request_id = _resolve_request_id(request)
    logger.warning("request_failed", request_id=request_id, code=presentation.code)

    return _error_response(
        presentation,
        message=exc.message or presentation.default_message,
        request_id=request_id,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Answer a bug with an opaque 500 and a loud, fully structured log record.

    Args:
        request: The request being answered, read only for its correlation ID.
        exc: The unhandled exception. Intentionally unread — it reaches the log
            through ``exc_info``, and reading it here would risk leaking its
            contents into the response. The parameter exists because Starlette's
            handler protocol passes it.

    Returns:
        A ``500`` carrying the fixed ``internal_error`` body.
    """
    request_id = _resolve_request_id(request)
    logger.exception("request_failed", request_id=request_id, code=_INTERNAL_ERROR.code)

    return _error_response(
        _INTERNAL_ERROR,
        message=_INTERNAL_ERROR.default_message,
        request_id=request_id,
    )


def _resolve_presentation(exc: WoolooException) -> _ErrorPresentation | None:
    """Select the HTTP presentation for an exception, honouring inheritance.

    Args:
        exc: The raised exception.

    Returns:
        The nearest mapped ancestor's presentation, or ``None`` when no ancestor
        is mapped.
    """
    for cls in type(exc).__mro__:
        presentation = _DOMAIN_PRESENTATIONS.get(cls)

        if presentation is not None:
            return presentation

    return None


def _resolve_request_id(request: Request) -> str | None:
    """Read the correlation ID off the request without trusting it to be there.

    Args:
        request: The request to read from.

    Returns:
        The middleware-assigned correlation ID, or ``None`` when the middleware
        did not run.
    """
    return getattr(request.state, "request_id", None)


def _error_response(
    presentation: _ErrorPresentation,
    *,
    message: str,
    request_id: str | None,
) -> JSONResponse:
    """Serialise one error into the API's single error shape.

    Args:
        presentation: Supplies the status code and machine-readable code.
        message: Human-readable text, already resolved against any default.
        request_id: Correlation ID, passed explicitly because
            :class:`ErrorResponse` defines no default for it — every error
            response carries the field, ``None`` included.

    Returns:
        A JSON response whose body is an :class:`ErrorResponse`.
    """
    body = ErrorResponse(
        code=presentation.code,
        message=message,
        request_id=request_id,
    )

    return JSONResponse(status_code=presentation.status_code, content=body.model_dump())

async def invalid_repository_name_handler(
    request: Request, exc: InvalidRepositoryName
) -> JSONResponse:
    """Answer a name that violates the OCI grammar with `400 validation_error`.

    Args:
        request: The request being answered, read only for its correlation ID.
        exc: The raised exception. Its message names the offending input, which is
            the client's own, so echoing it back discloses nothing they did not
            send and saves them guessing which part was rejected.

    Returns:
        A `400` carrying the standard error body.
    """
    return _repository_error_response(request, exc, _VALIDATION_ERROR)


async def repository_already_exists_handler(
    request: Request, exc: RepositoryAlreadyExists
) -> JSONResponse:
    """Answer a duplicate repository name with `409 conflict`.

    Args:
        request: The request being answered, read only for its correlation ID.
        exc: The raised exception, whose message names the taken name.

    Returns:
        A `409` carrying the standard error body.
    """
    return _repository_error_response(request, exc, _CONFLICT)


async def repository_not_found_handler(request: Request, exc: RepositoryNotFound) -> JSONResponse:
    """Answer an absent repository with `404 not_found`.

    Args:
        request: The request being answered, read only for its correlation ID.
        exc: The raised exception, whose message names what was looked up.

    Returns:
        A `404` carrying the standard error body.
    """
    return _repository_error_response(request, exc, _NOT_FOUND)


def _repository_error_response(
    request: Request,
    exc: RepositoryError,
    presentation: _ErrorPresentation,
) -> JSONResponse:
    """Log and render one repository failure, the way Epic 1 renders its own.

    Args:
        request: The request being answered, read only for its correlation ID.
        exc: The raised domain exception. Its `message` is used verbatim when
            supplied — like `WoolooException`'s, it is written by this codebase
            for an API consumer, not scraped off an arbitrary failure.
        presentation: The status code, machine-readable code and fallback wording
            for this class of failure.

    Returns:
        An :class:`ErrorResponse` body under the mapped status code.
    """
    request_id = _resolve_request_id(request)
    logger.warning("request_failed", request_id=request_id, code=presentation.code)

    return _error_response(
        presentation,
        message=exc.message or presentation.default_message,
        request_id=request_id,
    )
