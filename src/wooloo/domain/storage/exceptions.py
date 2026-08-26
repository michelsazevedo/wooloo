"""
Storage domain exceptions, raised by the domain and application layers.

"""


class StorageException(Exception):
    """Base class for every storage failure raised deliberately.

    Attributes:
        message: Caller-supplied detail, or `None` when the raiser had nothing to
            add beyond the exception type.
    """

    def __init__(self, message: str | None = None) -> None:
        """Initialize the exception.

        Args:
            message: Optional detail describing this specific occurrence, for
                example `"unknown key: 'sha256:deadbeef'"`. When given it is
                forwarded to :class:`Exception` so `str(exc)` and tracebacks stay
                informative; when omitted `str(exc)` is `""` rather than a
                misleading `"None"`.
        """
        if message is None:
            super().__init__()
        else:
            super().__init__(message)
        self.message = message


class BlobNotFound(StorageException):
    """
    A lookup, size query, or delete addressed a key that is not in storage. Blobs
    are physical content rather than rows with a soft-delete flag, so a missing
    key is a caller error worth surfacing instead of a silent no-op.

    """


class BlobAlreadyExists(StorageException):
    """
    A key was assigned to content that is not the content already stored under it.

    Not raised by the filesystem adapter: storage keys are content-addressed
    (sha256 of the blob), so a duplicate `put()` of the same key is by
    construction the same bytes and is handled as an idempotent no-op. This exists
    for a future backend or policy where key assignment is not content-derived.

    """
