"""
Tracer access for modules that record spans by hand.

"""

from opentelemetry.trace import Tracer
from opentelemetry.trace import get_tracer as _get_tracer


def get_tracer(name: str) -> Tracer:
    """Return a tracer for the calling module.

    Args:
        name: Instrumentation scope the spans are attributed to, conventionally the
            calling module's ``__name__``.

    Returns:
        A tracer bound to the globally registered provider.
    """
    return _get_tracer(name)
