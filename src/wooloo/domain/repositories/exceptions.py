"""
Repository domain exceptions, raised by the domain and application layers.

"""


class RepositoryError(Exception):
    """Base class for every repository rule violation raised deliberately.

    Attributes:
        message: Caller-supplied detail, or `None` when the raiser had nothing to
            add beyond the exception type.
    """

    def __init__(self, message: str | None = None) -> None:
        """Initialize the exception.

        Args:
            message: Optional detail describing this specific occurrence, for
                example `"invalid repository name: 'Library/Nginx'"`. When given
                it is forwarded to :class:`Exception` so `str(exc)` and
                tracebacks stay informative; when omitted `str(exc)` is `""`
                rather than a misleading `"None"`.
        """
        if message is None:
            super().__init__()
        else:
            super().__init__(message)
        self.message = message


class InvalidRepositoryName(RepositoryError):
    """
    A caller supplied a name no OCI client would accept, such as `Library/Nginx`
    or `acme//backend`, so it was rejected instead of being silently coerced into
    something storable.

    """


class RepositoryAlreadyExists(RepositoryError):
    """
    A repository with the requested name is already registered. Names are unique
    registry-wide, so the second creation attempt cannot be honoured without
    taking over an existing repository's identity.

    """


class RepositoryNotFound(RepositoryError):
    """
    The addressed repository has never been created, or has been soft-deleted and
    is therefore indistinguishable from one that never existed.

    """
