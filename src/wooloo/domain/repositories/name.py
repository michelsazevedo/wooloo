"""
The `RepositoryName` value object and the OCI naming grammar it enforces.

"""

import re
from dataclasses import dataclass
from typing import Final

from wooloo.domain.repositories.exceptions import InvalidRepositoryName

_COMPONENT_SEPARATOR: Final = "/"

_COMPONENT_PATTERN: Final = re.compile(r"^[a-z0-9]+((\.|_|__|-+)[a-z0-9]+)*$")
"""One path component: lowercase alphanumerics, optionally joined by a single
`.`, a single `_`, a double `__`, or a run of `-`. The anchors are the spec's
own; they are kept for fidelity and paired with `fullmatch` below, which also
closes `$`'s trailing-newline hole."""

_MAX_LENGTH: Final = 255
"""
Longest name this registry accepts, in characters.

"""

_MAX_ECHOED_INPUT: Final = 100
"""
Characters of a rejected name quoted back in the error message.

"""


def _truncate(value: str, limit: int = _MAX_ECHOED_INPUT) -> str:
    """Bound an attacker-controlled field before it reaches a message or log.

    Args:
        value: The raw value to bound.
        limit: Maximum characters kept verbatim.

    Returns:
        ``value`` unchanged if within ``limit``; otherwise the first ``limit``
        characters followed by ``…[<original length>]`` so the message still
        signals that truncation happened and how large the original was.
    """
    return value if len(value) <= limit else f"{value[:limit]}…[{len(value)}]"


def _is_valid_component(component: str) -> bool:
    """Check a single `/`-separated path component against the OCI grammar.

    Args:
        component: One component, already split off the full name.

    Returns:
        `True` if the component is legal on its own, `False` otherwise —
        including for the empty string, which is what a leading, trailing, or
        doubled separator produces.
    """
    return _COMPONENT_PATTERN.fullmatch(component) is not None


@dataclass(frozen=True, slots=True)
class RepositoryName:
    """A repository name proven to satisfy the OCI naming grammar.

    Attributes:
        value: The validated name, stripped of surrounding whitespace.
    """

    value: str

    def __post_init__(self) -> None:
        """Strip surrounding whitespace, then validate the result.

        Raises:
            InvalidRepositoryName: If the stripped name exceeds
                :data:`_MAX_LENGTH` characters, is empty, has an empty component
                (leading, trailing, or doubled `/`), or has a component that
                violates the grammar.
        """
        normalized = self.value.strip()
        if len(normalized) > _MAX_LENGTH:
            raise InvalidRepositoryName(
                f"repository name exceeds {_MAX_LENGTH} characters "
                f"(got {len(normalized)})"
            )

        components = normalized.split(_COMPONENT_SEPARATOR)
        if not all(_is_valid_component(component) for component in components):
            raise InvalidRepositoryName(
                f"invalid repository name: {_truncate(self.value)!r}"
            )
        object.__setattr__(self, "value", normalized)

    def __str__(self) -> str:
        """Return the validated name, so formatting a name yields the name itself.

        Returns:
            The stripped, validated repository name.
        """
        return self.value
