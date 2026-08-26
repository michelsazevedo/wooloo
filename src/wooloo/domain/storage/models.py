"""
The storage domain value objects.

"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class StoredBlob:
    """A piece of content the storage layer has persisted, addressed by `key`.

    The key is opaque to callers: it is whatever the backend assigned on write,
    and the only handle by which the content can be read back or deleted.

    `content_type` is `None` whenever the caller did not supply one. It is also
    never recovered afterwards — no read path returns a `StoredBlob`, so a caller
    coming back to an existing key has no way to learn the type it was written
    with.
    """

    key: str

    size: int

    content_type: str | None

    created_at: datetime
