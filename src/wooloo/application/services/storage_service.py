"""
Application-layer orchestration of blob storage.

"""

from collections.abc import AsyncIterator

from wooloo.domain.storage.contracts import BlobStorage
from wooloo.domain.storage.models import StoredBlob
from wooloo.infrastructure.logging.logger import logger


class StorageService:
    """Stores, retrieves, and removes blobs, recording each outcome.

    The `BlobStorage` is injected, so this service knows nothing about which
    backend is configured or how it is built — the same reason `HealthService`
    takes an `AsyncSession` rather than opening one.
    """

    def __init__(self, storage: BlobStorage) -> None:
        """Initialize the service.

        Args:
            storage: The persistence port used for every blob operation. Typically
                a `FilesystemBlobStorage`, but any object satisfying the protocol
                will do.
        """
        self._storage = storage

    async def store(
        self,
        content: AsyncIterator[bytes],
        *,
        content_type: str | None = None,
    ) -> StoredBlob:
        """Store a stream of bytes and report the blob it became.

        Args:
            content: The bytes to store, as a stream. Consumed once, to
                exhaustion.
            content_type: Optional media type recorded on the returned blob.

        Returns:
            The stored blob, carrying the backend-assigned key and byte count.
        """
        blob = await self._storage.put(content, content_type=content_type)

        logger.info("blob_stored", key=blob.key, size=blob.size)

        return blob

    async def retrieve(self, key: str) -> AsyncIterator[bytes]:
        """Open a stream over the content stored under `key`.

        Args:
            key: A key previously returned by `store()`.

        Returns:
            An iterator yielding the content in order, in bounded chunks.

        Raises:
            BlobNotFound: If `key` is unknown. Propagated unchanged from the size
                lookup and not logged — a stale key is the caller's 404, not a
                failure of this service.
        """
        size = await self._storage.size(key)

        logger.info("blob_retrieved", key=key, size=size)

        return await self._storage.get(key)

    async def remove(self, key: str) -> None:
        """Delete the content stored under `key`.

        Args:
            key: A key previously returned by `store()`.

        Returns:
            Nothing. Success is the absence of an exception.

        Raises:
            BlobNotFound: If `key` is unknown, including on a repeat delete.
                Propagated unchanged from the size lookup, which runs first so the
                byte count is still readable when the log line is written.
        """
        size = await self._storage.size(key)

        await self._storage.delete(key)

        logger.info("blob_deleted", key=key, size=size)
