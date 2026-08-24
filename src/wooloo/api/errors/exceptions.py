"""
Framework-agnostic exception hierarchy shared by every layer.

"""


class WoolooException(Exception):
    """Base class for every failure this application raises deliberately.

    Attributes:
        message: Caller-supplied detail, or ``None`` when the raiser had
            nothing to add beyond the exception type.
    """

    def __init__(self, message: str | None = None) -> None:
        """Initialize the exception.

        Args:
            message: Optional detail describing this specific occurrence, for
                example ``"Repository 'acme/backend-api' not found"``. When
                given it is also forwarded to :class:`Exception` so ``str(exc)``
                and tracebacks stay informative.
        """
        if message is None:
            super().__init__()
        else:
            super().__init__(message)
        self.message = message


class ValidationException(WoolooException):
    """
    The request was understood but its content is unusable.

    """


class NotFoundException(WoolooException):
    """
    The addressed resource does not exist, or is not visible to the caller.

    """


class ConflictException(WoolooException):
    """
    The request is valid but collides with the current state of a resource.

    """
