"""
The local filesystem adapter satisfying the `BlobStorage` port.

"""

import asyncio
import errno
import hashlib
import os
import re
import stat
import tempfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Final
from uuid import uuid4

from wooloo.domain.storage.exceptions import BlobNotFound, StorageException
from wooloo.domain.storage.models import StoredBlob

_KEY_PREFIX: Final = "sha256:"

_DIGEST_PATTERN: Final = re.compile(r"[0-9a-f]{64}")

_TMP_DIRECTORY_NAME: Final = "tmp"

_READ_CHUNK_SIZE: Final = 1024 * 1024

_HEALTH_PROBE_PAYLOAD: Final = b"wooloo"

_KEY_ECHO_LIMIT: Final = 80
"""
How much of a rejected key is quoted back in a message.

A well-formed key is 71 characters, so nothing legitimate is ever cut; the bound
exists only so a caller cannot inflate an error response — which echoes the message
verbatim — by sending a megabyte-long path parameter.

"""

_BLOB_OPEN_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
"""
How a stored blob is opened for reading.

`O_NOFOLLOW` makes a planted symlink an error instead of a redirection.
`O_NONBLOCK` matters for the same threat: opening a fifo would otherwise block until
a writer appears, parking a thread-pool thread forever on every read of that key. It
has no effect on a regular file, which is the only thing this adapter goes on to
read.

"""

_SYMLINK_OPEN_ERRNOS: Final = frozenset({errno.ELOOP, errno.EMLINK})
"""
What `O_NOFOLLOW` reports when the path is a symlink: `ELOOP` on Linux and macOS,
`EMLINK` on some BSDs. Neither is mapped to a named `OSError` subclass.

"""


class _NotARegularFile(OSError):
    """
    Something occupies a blob's path, but it is not a regular file — a directory,
    a fifo, a device node. Raised after the descriptor is open, since only an
    `fstat` on the descriptor itself answers the question without a second lookup
    that could race the first.

    """


def _unknown_key_message(key: str) -> str:
    """Phrase the "not stored" message, bounding how much of the key it repeats.

    Args:
        key: The key that was not found, straight from a caller this adapter does
            not control.

    Returns:
        The message, quoting at most `_KEY_ECHO_LIMIT` characters of `key` and
        saying so when it had to cut. `repr` escapes control characters, so the
        text is safe to place in a log line or a JSON body.
    """
    if len(key) <= _KEY_ECHO_LIMIT:
        return f"unknown key: {key!r}"

    return f"unknown key: {key[:_KEY_ECHO_LIMIT]!r} (truncated)"


def _is_absent_blob(exc: OSError) -> bool:
    """Decide whether a failed open means "no blob here" or "the store is broken".

    Args:
        exc: The error raised while opening a content-addressed path.

    Returns:
        `True` when nothing readable as a blob is at that path — it is missing, a
        path component is not a directory, `O_NOFOLLOW` refused a symlink, or what
        is there is not a regular file. `False` for everything else, such as a
        permission or I/O error, which is a broken store rather than an absence
        and must not be flattened into a 404.
    """
    if isinstance(exc, FileNotFoundError | NotADirectoryError | _NotARegularFile):
        return True

    return exc.errno in _SYMLINK_OPEN_ERRNOS


def _open_stored_blob(path: Path) -> BinaryIO:
    """Open a blob for reading, refusing anything that is not a regular file.

    Args:
        path: The content-addressed path to open.

    Returns:
        A binary read handle on the file that was at `path` at the moment of the
        open. The caller owns it and must close it.

    Raises:
        _NotARegularFile: If the descriptor turned out not to name a regular file.
        OSError: Whatever the open reported, including `ELOOP`/`EMLINK` for a
            symlink refused by `O_NOFOLLOW`.
    """
    descriptor = os.open(path, _BLOB_OPEN_FLAGS)

    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _NotARegularFile(errno.EINVAL, "not a regular file", str(path))
    except BaseException:
        os.close(descriptor)
        raise

    return os.fdopen(descriptor, "rb")


def _lstat_or_none(path: Path) -> os.stat_result | None:
    """Stat a path without following a symlink, treating absence as a value.

    Args:
        path: The path to stat.

    Returns:
        The path's own metadata — the link's, never its target's — or `None` when
        nothing is there. A permission or I/O error is *not* absence and is left
        to propagate.
    """
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _is_stored_blob(path: Path) -> bool:
    """Report whether a path holds a regular file, without following a symlink.

    Args:
        path: The path to inspect.

    Returns:
        `True` only for a regular file. A symlink is `False` whatever it points
        at, and so is a path that cannot be stat'ed at all — `exists()` promises a
        bool under every condition.
    """
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _write_and_remove_probe(probe: Path) -> None:
    """Prove a directory accepts writes by creating and removing one file.

    Args:
        probe: The file to write and then remove.

    Raises:
        OSError: If the write or the removal failed. A probe that cannot be cleaned
            up is a failed health check, not a successful one, so the removal's
            error is deliberately not swallowed.
    """
    try:
        probe.write_bytes(_HEALTH_PROBE_PAYLOAD)
    finally:
        probe.unlink(missing_ok=True)


async def _stream_handle(handle: BinaryIO) -> AsyncIterator[bytes]:
    """Yield an already-open file's contents in bounded chunks, then close it.

    Args:
        handle: An open binary read handle, whose ownership passes to this
            generator: it is closed when the stream is exhausted, closed early, or
            abandoned.

    Yields:
        Successive chunks of at most `_READ_CHUNK_SIZE` bytes, in order.
    """
    try:
        while chunk := await asyncio.to_thread(handle.read, _READ_CHUNK_SIZE):
            yield chunk
    finally:
        handle.close()


class FilesystemBlobStorage:
    """
    Stores blobs as files under a content-addressed directory tree.

    """

    def __init__(self, storage_root: Path) -> None:
        """Initialize the adapter.

        Args:
            storage_root: Directory the blob tree and its scratch directory live
                under.
        """
        self._storage_root = storage_root
        self._tmp_directory = storage_root / _TMP_DIRECTORY_NAME

    async def put(
        self,
        content: AsyncIterator[bytes],
        *,
        content_type: str | None = None,
    ) -> StoredBlob:
        """Store the content of this stream and return the blob it became.

        Args:
            content: The bytes to store, consumed once to exhaustion. Never
                accumulated: each chunk is hashed and written straight through, so
                peak memory is one chunk regardless of payload size.
            content_type: Optional media type, echoed onto the returned blob and
                not persisted.

        Returns:
            The stored blob, with `size` and `created_at` read back from the file
            that now holds the content — the original file on a repeat call, whose
            mtime is therefore the first write's, not this one's.

        Raises:
            StorageException: If something that is not a regular file already
                occupies the content-addressed path — a symlink another local user
                planted at a digest they predicted, say. Treating that as "already
                stored" would discard the bytes just hashed and report a foreign
                file's stats under a key derived from entirely different content,
                so it is refused loudly instead: the storage tree is corrupt or
                tampered with, which is neither an absence nor a conflict.
            OSError: If the filesystem refuses the write. Nothing is ever published
                at the content-addressed path unless the rename succeeded, but a
                failure *after* the content has been spooled — from `makedirs`,
                `rename`, or the stat — leaves the scratch file under `tmp/` for a
                future GC to collect. Only a failure during spooling cleans up
                after itself.
        """
        tmp_path, digest = await self._spool(content)
        target = self._shard_path(digest)
        existing = await asyncio.to_thread(_lstat_or_none, target)

        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise StorageException(
                f"corrupt storage tree: {target} exists and is not a regular file"
            )

        if existing is None:
            await asyncio.to_thread(os.makedirs, target.parent, exist_ok=True)
            published = await asyncio.to_thread(os.lstat, tmp_path)
            await asyncio.to_thread(os.rename, tmp_path, target)
        else:
            await asyncio.to_thread(os.unlink, tmp_path)
            published = existing

        return StoredBlob(
            key=f"{_KEY_PREFIX}{digest}",
            size=published.st_size,
            content_type=content_type,
            created_at=datetime.fromtimestamp(published.st_mtime, tz=UTC),
        )

    async def get(self, key: str) -> AsyncIterator[bytes]:
        """Open a stream over the content stored under `key`.

        Args:
            key: A key previously returned by `put()`.

        Returns:
            An iterator yielding the content in order, in bounded chunks.

        Raises:
            BlobNotFound: If `key` is unknown, could never have been issued by
                `put()`, or names something that is not a regular file — a symlink
                `O_NOFOLLOW` refused, for instance, which is not a blob no matter
                what it points at. Raised while this call is being awaited, before
                any iterator exists, so a caller that has not yet started streaming
                can still choose its response status.
            OSError: If the open failed for a reason that is not absence, such as a
                permission or I/O error. That is a broken store, and reporting it
                as a `404` would hide an outage behind a routine answer.
        """
        path = self._resolve(key)
        if path is None:
            raise BlobNotFound(_unknown_key_message(key))

        try:
            handle = await asyncio.to_thread(_open_stored_blob, path)
        except OSError as exc:
            if not _is_absent_blob(exc):
                raise

            raise BlobNotFound(_unknown_key_message(key)) from exc

        return _stream_handle(handle)

    async def exists(self, key: str) -> bool:
        """Report whether anything is stored under `key`.

        Args:
            key: The key to check.

        Returns:
            `True` if content is stored under `key`, `False` otherwise, including
            for a key `put()` could never have issued and for a path holding
            something other than a regular file.
        """
        path = self._resolve(key)
        if path is None:
            return False

        return await asyncio.to_thread(_is_stored_blob, path)

    async def size(self, key: str) -> int:
        """Report the size in bytes of the content stored under `key`.

        Args:
            key: A key previously returned by `put()`.

        Returns:
            The stored byte count, of the file itself: metadata is read with
            `os.lstat`, so a symlink can never contribute its target's size.

        Raises:
            BlobNotFound: If `key` is unknown, could never have been issued by
                `put()`, or names something that is not a regular file.
        """
        path = self._resolve(key)
        if path is None:
            raise BlobNotFound(_unknown_key_message(key))

        try:
            entry = await asyncio.to_thread(os.lstat, path)
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise BlobNotFound(_unknown_key_message(key)) from exc

        if not stat.S_ISREG(entry.st_mode):
            raise BlobNotFound(_unknown_key_message(key))

        return entry.st_size

    async def delete(self, key: str) -> None:
        """Remove the content stored under `key`.

        Args:
            key: A key previously returned by `put()`.

        Returns:
            Nothing.

        Raises:
            BlobNotFound: If `key` is unknown, including on a repeat delete. Decided
                by the unlink itself rather than by a preceding existence check, so
                two concurrent deletes cannot both report success. `os.unlink` never
                follows a symlink, so this needs no guard of its own: a link planted
                at a blob's path is what gets removed, never whatever it points at.
        """
        path = self._resolve(key)
        if path is None:
            raise BlobNotFound(_unknown_key_message(key))

        try:
            await asyncio.to_thread(os.unlink, path)
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise BlobNotFound(_unknown_key_message(key)) from exc

    async def check_health(self) -> bool:
        """Report whether the storage root exists and accepts writes.

        Returns:
            `True` if the root is present and a write-then-delete probe succeeded,
            `False` on any `OSError`.
        """
        probe = self._storage_root / f".health-{uuid4().hex}"

        try:
            if not await asyncio.to_thread(self._storage_root.is_dir):
                return False

            await asyncio.to_thread(_write_and_remove_probe, probe)
        except OSError:
            return False

        return True

    async def _spool(self, content: AsyncIterator[bytes]) -> tuple[Path, str]:
        """Write a stream to a scratch file, hashing it on the way through.

        Args:
            content: The bytes to spool, consumed to exhaustion.

        Returns:
            The scratch file's path and the hex sha256 digest of everything written
            to it. The caller owns the file from here and must move or remove it.

        Raises:
            BaseException: Whatever the source stream or the filesystem raised,
                re-raised after the partial scratch file is removed.
        """
        await asyncio.to_thread(os.makedirs, self._tmp_directory, exist_ok=True)

        descriptor, name = await asyncio.to_thread(
            tempfile.mkstemp, dir=self._tmp_directory, suffix=".part"
        )
        tmp_path = Path(name)
        digest = hashlib.sha256()

        try:
            handle = await asyncio.to_thread(os.fdopen, descriptor, "wb")
            try:
                async for chunk in content:
                    digest.update(chunk)
                    await asyncio.to_thread(handle.write, chunk)

                await asyncio.to_thread(handle.flush)
                await asyncio.to_thread(os.fsync, handle.fileno())
            finally:
                await asyncio.to_thread(handle.close)
        except BaseException:
            await asyncio.to_thread(tmp_path.unlink, missing_ok=True)
            raise

        return tmp_path, digest.hexdigest()

    def _resolve(self, key: str) -> Path | None:
        """Translate a key into the path that would hold its content.

        Args:
            key: The key to translate, from a caller this adapter does not control.

        Returns:
            The sharded path, or `None` if `key` is not shaped like one `put()`
            issues — malformed input is absence, not an error, so every read method
            can answer it in its own vocabulary.
        """
        if not key.startswith(_KEY_PREFIX):
            return None

        digest = key.removeprefix(_KEY_PREFIX)
        if not _DIGEST_PATTERN.fullmatch(digest):
            return None

        return self._shard_path(digest)

    def _shard_path(self, digest: str) -> Path:
        """Lay a hex digest out across two shard directories.

        Args:
            digest: A 64-character lowercase hex sha256 digest.

        Returns:
            `<storage_root>/<digest[:2]>/<digest[2:4]>/<digest[4:]>`.
        """
        return self._storage_root / digest[:2] / digest[2:4] / digest[4:]
