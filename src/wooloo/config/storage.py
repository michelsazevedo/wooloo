"""
Recognised storage backend names.

"""

from typing import Final

_VALID_STORAGE_BACKENDS: Final = frozenset({"filesystem", "s3"})
