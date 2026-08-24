"""
Process-wide logging configuration.

Wires the standard library's :mod:`logging` and ``structlog`` into a single
JSON-rendering pipeline on stdout, so that records emitted by application code and
by third-party libraries that log through :mod:`logging` (uvicorn, SQLAlchemy) come
out in the same machine-parseable shape.

"""

import logging
import sys
from importlib.metadata import version
from typing import Final

import structlog
from structlog.tracebacks import ExceptionDictTransformer
from structlog.typing import EventDict, Processor, WrappedLogger

from wooloo.config.settings import get_settings

_SERVICE_NAME: Final = "wooloo"
"""
Value of the ambient ``service`` log field, by which this process's records are
told apart from every other service writing into the same log store.

"""

_DISTRIBUTION_NAME: Final = "wooloo"
"""
Installed distribution whose metadata supplies the ambient ``version`` log field.

Coincides with :data:`_SERVICE_NAME` today, but they answer different questions —
which service emitted the record, versus where its version number is read from —
and only one of them is the name a packaging change could move.

"""


class _ServiceMetadataProcessor:
    """Stamps service identity onto every event dict passing through structlog.

    Attributes:
        service: Value written to the ``service`` field.
        version: Value written to the ``version`` field.
    """

    def __init__(self, service: str, service_version: str) -> None:
        """Capture the identity to stamp.

        Args:
            service: The service name.
            service_version: The resolved distribution version.
        """
        self.service = service
        self.version = service_version

    def __call__(
        self, _logger: WrappedLogger, _method_name: str, event_dict: EventDict
    ) -> EventDict:
        """Add the service fields to one record, without overwriting a caller.

        ``setdefault`` rather than assignment, so an explicit keyword at the call
        site — ``logger.info("application_started", service=...)`` — still wins.
        That mirrors how ``merge_contextvars`` treats bound context and is what
        keeps the two sources from ever producing a conflict.

        Args:
            _logger: The wrapped logger. Unused.
            _method_name: The log method's name. Unused.
            event_dict: The record being built.

        Returns:
            The same dict, with ``service`` and ``version`` present.
        """
        event_dict.setdefault("service", self.service)
        event_dict.setdefault("version", self.version)

        return event_dict


def _build_shared_processors() -> tuple[Processor, ...]:
    """Return the processors applied to every record, structlog-native or foreign.

    A function rather than a module constant because one member needs the installed
    distribution's version, and resolving that at import time would give this module
    an import-time failure mode it deliberately does not have — nothing here touches
    global state or package metadata until :func:`configure_logging` is called.
    """
    return (
        structlog.contextvars.merge_contextvars,
        
        _ServiceMetadataProcessor(_SERVICE_NAME, version(_DISTRIBUTION_NAME)),
        
        structlog.stdlib.ExtraAdder(),
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    )

_RENDER_PROCESSORS: tuple[Processor, ...] = (
    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
    structlog.processors.ExceptionRenderer(
        
        ExceptionDictTransformer(show_locals=False),
    ),
    structlog.processors.JSONRenderer(),
)
"""
Tail of the chain, run inside the formatter so that structlog-originated and
foreign records converge on the same renderer.

"""


def configure_logging() -> None:
    """Configure stdlib logging and structlog to emit JSON on stdout.

    Raises:
        importlib.metadata.PackageNotFoundError: If ``wooloo`` is not installed in
            the running environment, leaving no metadata to read the ambient
            ``version`` field from.
        pydantic.ValidationError: If settings cannot be loaded, via
            :func:`~wooloo.config.settings.get_settings`.
    """
    shared_processors = _build_shared_processors()

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=_RENDER_PROCESSORS,
    )
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)

    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    logging.getLogger("uvicorn.access").disabled = True

    level = _resolve_level(get_settings().log_level)
    handler.setLevel(level)
    logging.getLogger("wooloo").setLevel(level)
    root_logger.setLevel(logging.INFO)

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        
        cache_logger_on_first_use=False,
    )


def _resolve_level(level: str) -> int:
    """Translate a level name into its numeric :mod:`logging` counterpart.

    Args:
        level: A level name such as ``"INFO"``, matched case-insensitively.

    Returns:
        The numeric level accepted by :meth:`logging.Logger.setLevel`.

    Raises:
        KeyError: If ``level`` is not a known level name. ``Settings`` validates
            the value at construction time, so reaching this is a configuration
            bug rather than an operator error.
    """
    return logging.getLevelNamesMapping()[level.upper()]
