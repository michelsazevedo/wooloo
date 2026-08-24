"""
Standard error response body.

"""

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    """The one JSON shape every API error response uses.

    A 400, 404, 409 and 500 all serialise to these three keys and nothing else,
    which lets a client handle any failure with a single parser and lets support
    correlate a user-reported error with server logs via ``request_id``.

    Attributes:
        code: Short machine-readable error code that clients may branch on,
            e.g. ``not_found``. Stable across message wording changes.
        message: Human-readable description of what went wrong, safe to show to
            an API consumer.
        request_id: The ID correlating this response with the server-side log
            line for the same request. Populated on *every* error, not just
            ``500``, so a client can always quote it in a support ticket. It is
            required — callers state it explicitly — and ``None`` exists only as
            a defensive fallback for the theoretical case where no request ID was
            resolved, which the request-logging middleware makes impossible in
            practice.
    """

    code: str

    message: str

    request_id: str | None
