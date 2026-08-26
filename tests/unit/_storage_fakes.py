"""
An in-memory `BlobStorage` double, used by the `StorageService` unit tests.

"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

from wooloo.domain.storage.contracts import BlobStorage
from wooloo.domain.storage.exceptions import BlobNotFound
from wooloo.domain.storage.models import StoredBlob

FIXED_CREATED_AT = datetime(2026, 1, 15, 9, 30, tzinfo=UTC)
"""
A fixed, timezone-aware write moment. Nothing under test reads it, so a moving
clock could only introduce nondeterminism without buying an assertion.

"""


def make_stored_blob(key: str, *, size: int, content_type: str | None = None) -> StoredBlob:
    """Build the blob a test hands back through the double.

    Args:
        key: The backend-assigned key, deliberately unrelated to the content the
            test streams in.
        size: The byte count carried on the blob. Tests choose one that disagrees
            with the length of the content they stream, so an assertion can tell
            which of the two a log line read.
        content_type: The media type echoed onto the blob.

    Returns:
        A fully populated blob with a fixed timestamp.
    """
    return StoredBlob(key=key, size=size, content_type=content_type, created_at=FIXED_CREATED_AT)


async def byte_stream(*chunks: bytes) -> AsyncIterator[bytes]:
    """Yield `chunks` in order, standing in for an upload's request body.

    Args:
        chunks: The pieces to yield, one per iteration. Passing more than one
            keeps the stream genuinely multi-chunk, so a consumer that reads only
            the first is visible.

    Yields:
        Each chunk in turn.
    """
    for chunk in chunks:
        yield chunk


@dataclass(frozen=True)
class PutCall:
    """
    One recorded `BlobStorage.put` call, as the port received it.

    Attributes:
        content: Every byte the double read off the stream, concatenated.
        content_type: The media type the port was called with.
    """

    content: bytes

    content_type: str | None


class FakeBlobStorage:
    """A `BlobStorage` that records what it was asked and answers as configured.

    Attributes:
        put_calls: The `PutCall`s the port received, in order.
        get_calls: Keys passed to `get`, in order.
        exists_calls: Keys passed to `exists`, in order.
        size_calls: Keys passed to `size`, in order.
        delete_calls: Keys passed to `delete`, in order. Recorded before the call
            can fail, so a test can assert on a delete that raised.
        check_health_calls: How many times the readiness probe was awaited.
    """

    def __init__(
        self,
        *,
        put_returns: StoredBlob | None = None,
        get_returns: AsyncIterator[bytes] | None = None,
        exists_returns: bool = False,
        size_returns: int | None = None,
        size_raises: BaseException | None = None,
        delete_succeeds: bool | None = None,
        check_health_returns: bool = True,
    ) -> None:
        """Configure the double's answers.

        Args:
            put_returns: The blob `put` yields. Independent of the content it was
                called with, on purpose: a test proves which value a log line read
                by making the two disagree. Left `None`, a `put` call is an error.
            get_returns: The stream `get` yields. Single-use, like the real one.
                Left `None`, every key is unknown to `get`.
            exists_returns: What `exists` reports for any key.
            size_returns: The byte count `size` yields. Left `None`, every key is
                unknown to `size`.
            size_raises: Raised by `size` instead of returning or reporting a miss,
                so a test can assert the very exception object propagated. The call
                is still recorded before the raise.
            delete_succeeds: `True` removes the key, `False` reports it as already
                absent. Left `None`, a `delete` call is an error.
            check_health_returns: The readiness verdict. Unused by
                `StorageService`, which never probes; it exists because the port
                does.
        """
        self._put_returns = put_returns
        self._get_returns = get_returns
        self._exists_returns = exists_returns
        self._size_returns = size_returns
        self._size_raises = size_raises
        self._delete_succeeds = delete_succeeds
        self._check_health_returns = check_health_returns

        self.put_calls: list[PutCall] = []
        self.get_calls: list[str] = []
        self.exists_calls: list[str] = []
        self.size_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.check_health_calls = 0

    async def put(
        self,
        content: AsyncIterator[bytes],
        *,
        content_type: str | None = None,
    ) -> StoredBlob:
        """Drain the stream, record what arrived, and return the configured blob.

        Args:
            content: The bytes to store, consumed here exactly as the real adapter
                consumes them.
            content_type: Optional media type, recorded verbatim.

        Returns:
            The configured `put_returns`.

        Raises:
            AssertionError: If no outcome was configured, meaning the service
                reached persistence unexpectedly.
        """
        buffer = bytearray()
        async for chunk in content:
            buffer.extend(chunk)

        self.put_calls.append(PutCall(content=bytes(buffer), content_type=content_type))

        if self._put_returns is None:
            raise AssertionError("put() was called but no outcome was configured")

        return self._put_returns

    async def get(self, key: str) -> AsyncIterator[bytes]:
        """Record the key and hand back the configured stream.

        Args:
            key: The key the service asked to read.

        Returns:
            The configured `get_returns`.

        Raises:
            BlobNotFound: If no stream was configured, the port's own way of
                spelling an unknown key.
        """
        self.get_calls.append(key)

        if self._get_returns is None:
            raise BlobNotFound(f"unknown key: {key!r}")

        return self._get_returns

    async def exists(self, key: str) -> bool:
        """Record the key and report the configured verdict.

        Args:
            key: The key the service checked.

        Returns:
            The configured `exists_returns`.
        """
        self.exists_calls.append(key)
        return self._exists_returns

    async def size(self, key: str) -> int:
        """Record the key, then raise or report the configured byte count.

        Args:
            key: The key the service measured.

        Returns:
            The configured `size_returns`.

        Raises:
            BaseException: The configured `size_raises`, if any.
            BlobNotFound: If no byte count was configured, the port's own way of
                spelling an unknown key.
        """
        self.size_calls.append(key)

        if self._size_raises is not None:
            raise self._size_raises

        if self._size_returns is None:
            raise BlobNotFound(f"unknown key: {key!r}")

        return self._size_returns

    async def delete(self, key: str) -> None:
        """Record the key, then remove it or report it as already absent.

        Args:
            key: The key the service asked to remove.

        Returns:
            Nothing, as on the port: success is the absence of an exception.

        Raises:
            BlobNotFound: If `delete_succeeds` is `False`.
            AssertionError: If no outcome was configured, meaning the service
                reached persistence unexpectedly.
        """
        self.delete_calls.append(key)

        if self._delete_succeeds is None:
            raise AssertionError("delete() was called but no outcome was configured")

        if not self._delete_succeeds:
            raise BlobNotFound(f"unknown key: {key!r}")

    async def check_health(self) -> bool:
        """Count the probe and report the configured verdict.

        Returns:
            The configured `check_health_returns`.
        """
        self.check_health_calls += 1
        return self._check_health_returns


def _assert_protocol_conformance(storage: FakeBlobStorage) -> BlobStorage:
    """Make the double's conformance to the port a mypy-checked fact.

    Args:
        storage: The double to check.

    Returns:
        The same object, seen as the port.
    """
    return storage
