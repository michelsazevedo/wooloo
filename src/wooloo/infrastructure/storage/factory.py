"""
The one place configuration becomes a concrete `BlobStorage`.

"""

from pathlib import Path
from typing import Final

from wooloo.config.settings import Settings
from wooloo.domain.storage.contracts import BlobStorage
from wooloo.infrastructure.storage.filesystem import FilesystemBlobStorage

_FILESYSTEM_BACKEND: Final = "filesystem"


def build_blob_storage(settings: Settings) -> BlobStorage:
    """Build the blob storage adapter named by the settings.

    Args:
        settings: Application settings whose `storage_backend` names the adapter
            and whose `storage_root` configures the filesystem one.

    Returns:
        The adapter for the configured backend.

    Raises:
        NotImplementedError: If the configured backend is one of the reserved
            names no adapter backs yet. Every non-filesystem name reaches this,
            which is exhaustive rather than approximate: `Settings` has already
            rejected anything outside the recognised set, so what arrives here is
            exactly `s3`, `minio`, or `gcs`. Raised loudly instead of falling back
            to the filesystem, which would quietly write a production registry's
            blobs to local disk.
    """
    if settings.storage_backend == _FILESYSTEM_BACKEND:
        return FilesystemBlobStorage(Path(settings.storage_root))

    raise NotImplementedError(
        f"storage backend {settings.storage_backend!r} is a recognised name with no "
        f"adapter behind it yet; only {_FILESYSTEM_BACKEND!r} is implemented"
    )
