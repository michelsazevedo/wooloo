"""
The `BlobStorage` port every storage backend implements.

"""

from collections.abc import AsyncIterator
from typing import Protocol

from wooloo.domain.storage.models import StoredBlob


class BlobStorage(Protocol):
    """
    The persistence contract for blob content.

    """

    async def put(
        self,
        content: AsyncIterator[bytes],
        *,
        content_type: str | None = None,
    ) -> StoredBlob:
        """Store the content of this stream and return the blob it became.

        Args:
            content: The bytes to store, as a stream. Consumed once, to
                exhaustion.
            content_type: Optional media type to record on the returned blob. It
                is echoed back here and nowhere else — no method on this contract
                reads it again afterwards.

        Returns:
            The stored blob: its computed key, the number of bytes consumed from
            the stream, the `content_type` passed in, and when the content was
            first stored — a repeat call reports the original write's timestamp,
            not this call's.

        Raises:
            Nothing contractual. In particular, a repeat call whose content
            hashes to an already-stored key is an idempotent no-op that still
            returns a correct `StoredBlob`; implementations must not raise
            `BlobAlreadyExists` for that case, because a content-derived key
            makes the incoming bytes and the stored bytes the same bytes by
            construction. `BlobAlreadyExists` remains in the vocabulary for a
            future backend whose keys are not content-derived, where a collision
            would be a genuine conflict; no backend in this epic raises it.
        """
        ...

    async def get(self, key: str) -> AsyncIterator[bytes]:
        """Open a stream over the content stored under `key`.

        Args:
            key: A key previously returned by `put()`.

        Returns:
            An iterator yielding the content in order, in bounded chunks rather
            than as one materialised payload.

        Raises:
            BlobNotFound: If `key` is unknown. Raised eagerly — while this call is
                being awaited, before the iterator is returned — and never lazily
                on the first chunk. The caller is an HTTP route that cannot change
                its status code once the response body has begun streaming, so a
                late raise would corrupt an in-flight 200 instead of producing a
                clean 404. This rules out implementing the method as an async
                generator, whose body does not run until first iteration.
        """
        ...

    async def exists(self, key: str) -> bool:
        """Report whether anything is stored under `key`.

        Args:
            key: The key to check. A key that `put()` could never have issued is
                simply absent, not a malformed input.

        Returns:
            `True` if content is stored under `key`, `False` otherwise.

        Raises:
            Nothing contractual. In particular an unknown key is reported by the
            return value rather than by a `BlobNotFound`.
        """
        ...

    async def size(self, key: str) -> int:
        """Report the size in bytes of the content stored under `key`.

        Args:
            key: A key previously returned by `put()`.

        Returns:
            The stored byte count, matching the `size` on the `StoredBlob` that
            `put()` returned for this key.

        Raises:
            BlobNotFound: If `key` is unknown. Unlike `exists()`, absence is an
                error here rather than a value, since no integer could stand for
                "not stored" without colliding with a legitimately empty blob.
        """
        ...

    async def delete(self, key: str) -> None:
        """Remove the content stored under `key`.

        Args:
            key: A key previously returned by `put()`.

        Returns:
            Nothing. Success is the absence of an exception.

        Raises:
            BlobNotFound: If `key` is unknown, including on a repeat delete of a
                key this method already removed. This is a deliberate contrast
                with `RepositoryStore.delete()`, which is idempotent: a repository
                is a row carrying a soft-delete flag, so deleting it twice is
                harmless bookkeeping. A blob is physical content, so deleting
                something that is not there means the caller is working from a
                stale key — worth surfacing rather than absorbing.
        """
        ...

    async def check_health(self) -> bool:
        """Report whether the backend is ready to serve reads and writes.

        This method exists so `HealthService` can include storage in the readiness
        response without knowing which backend is configured. Each implementation
        decides what "ready" means for itself — one may confirm its storage
        location is present and accepts a write, another may issue a lightweight
        metadata call to a remote service — and answers with a plain bool, keeping
        backend-specific probes out of the health logic.

        Returns:
            `True` if the backend is reachable and writable, `False` otherwise.

        Raises:
            Nothing contractual, under any condition. A probe that fails — for any
            reason, including one the implementation did not anticipate — is
            reported as `False`. A readiness check that could itself raise would
            take down the endpoint whose entire job is to report that something is
            wrong.
        """
        ...
