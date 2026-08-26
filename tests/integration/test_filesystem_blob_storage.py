"""
Integration tests for `FilesystemBlobStorage` against a real temp directory.

"""

import hashlib
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from wooloo.domain.storage.exceptions import BlobNotFound
from wooloo.infrastructure.storage.filesystem import FilesystemBlobStorage

TMP_DIRECTORY_NAME = "tmp"
"""
The scratch directory `put()` spools through, restated rather than imported.

"""

KNOWN_PAYLOAD = b"wooloo"
"""
The fixed input whose digest and on-disk layout are written out as literals below.

"""

KNOWN_DIGEST = "33ca1130bb7975f4b7ca724e9d265be57f1a9a33b05f9ced9a663db1d40abd12"
"""
sha256 of `KNOWN_PAYLOAD`, computed outside this suite with `shasum -a 256`.

"""

KNOWN_SHARD_COMPONENTS = (
    "33",
    "ca",
    "1130bb7975f4b7ca724e9d265be57f1a9a33b05f9ced9a663db1d40abd12",
)
"""
The path `KNOWN_DIGEST` must be laid out across, spelled out instead of sliced.

"""

EMPTY_DIGEST = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
"""
sha256 of no bytes at all, likewise computed outside this suite.

"""

LARGE_PAYLOAD_SIZE = 5 * 1024 * 1024 + 7
"""
Big enough to cross the adapter's 1 MiB read chunk several times, and deliberately
not a multiple of either chunk size so a dropped final chunk cannot hide.

"""

UPLOAD_CHUNK_SIZE = 8 * 1024
"""
The slice size the large payload is fed in, keeping the streaming path multi-chunk.

"""

MTIME_GUARD_SECONDS = 1.1
"""
A real pause between two writes, longer than the coarsest filesystem mtime
resolution likely to be under the temp directory, so "the mtime did not move" is a
statement about the adapter rather than about the clock.

"""

MALFORMED_KEYS = [
    "",
    "sha256:",
    KNOWN_DIGEST,
    f"sha256:{KNOWN_DIGEST[:-1]}",
    f"sha256:{KNOWN_DIGEST}0",
    f"sha256:{KNOWN_DIGEST.upper()}",
    "sha256:" + "z" * 64,
    "sha256:../../../etc/passwd",
    f"sha256:{KNOWN_DIGEST}/../../../etc/passwd",
    "md5:" + "0" * 32,
]
"""
Keys `put()` could never have issued: no prefix, wrong digest length, wrong case,
non-hex, path traversal, another algorithm.

"""


async def stream(*chunks: bytes) -> AsyncIterator[bytes]:
    """Present chunks as the stream `put()` consumes.

    Args:
        chunks: The chunks to yield, in order. None means an empty blob.

    Yields:
        Each chunk, unchanged.
    """
    for chunk in chunks:
        yield chunk


async def chunked(payload: bytes, size: int) -> AsyncIterator[bytes]:
    """Slice a payload into a multi-chunk stream.

    Args:
        payload: The bytes to feed.
        size: The slice size; the final chunk is short unless it divides evenly.

    Yields:
        Successive slices of `payload`, in order.
    """
    for start in range(0, len(payload), size):
        yield payload[start : start + size]


async def failing_stream(prefix: bytes, error: Exception) -> AsyncIterator[bytes]:
    """Yield some bytes, then fail the way an aborted upload does.

    Args:
        prefix: The bytes that reach the scratch file before the failure.
        error: The failure to raise once `prefix` has been consumed.

    Yields:
        `prefix`, and nothing after it.
    """
    yield prefix
    raise error


async def collect(content: AsyncIterator[bytes]) -> bytes:
    """Drain a stream into the bytes it carried.

    Args:
        content: The stream to exhaust.

    Returns:
        Every chunk concatenated, in order.
    """
    return b"".join([chunk async for chunk in content])


def key_for(payload: bytes) -> str:
    """Compute the key `put()` must issue for a payload.

    Independent of the adapter: this is the digest a caller would compute for
    itself, which is the whole point of a content-addressed key.

    Args:
        payload: The content to address.

    Returns:
        `sha256:<64 lowercase hex>`.
    """
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def shard_path(root: Path, digest: str) -> Path:
    """Spell out the layout the adapter contracts to, without importing its helper.

    Args:
        root: The storage root.
        digest: A 64-character lowercase hex digest.

    Returns:
        `<root>/<digest[:2]>/<digest[2:4]>/<digest[4:]>`.
    """
    return root / digest[:2] / digest[2:4] / digest[4:]


def stored_files(root: Path) -> list[Path]:
    """List every file that is actually published as content.

    Args:
        root: The storage root.

    Returns:
        Each file under `root` outside the scratch directory, sorted. Health
        probes clean up after themselves, so anything here is a blob.
    """
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and TMP_DIRECTORY_NAME not in path.relative_to(root).parts
    )


def scratch_files(root: Path) -> list[Path]:
    """List what is left lying in the scratch directory.

    Args:
        root: The storage root.

    Returns:
        Each file under `<root>/tmp/`, sorted, or an empty list if the directory
        was never created.
    """
    scratch = root / TMP_DIRECTORY_NAME

    if not scratch.is_dir():
        return []

    return sorted(path for path in scratch.rglob("*") if path.is_file())


@pytest.fixture
def storage(tmp_path: Path) -> FilesystemBlobStorage:
    """
    Return the adapter under test, rooted at this test's own temp directory.

    """
    return FilesystemBlobStorage(tmp_path)


async def test_put_then_get_round_trips_the_content_byte_for_byte(
    storage: FilesystemBlobStorage,
) -> None:
    """
    The key is the content's own sha256, computed here without asking the adapter.

    """
    blob = await storage.put(stream(KNOWN_PAYLOAD))

    assert blob.key == key_for(KNOWN_PAYLOAD)
    assert blob.size == len(KNOWN_PAYLOAD)
    assert blob.content_type is None
    assert blob.created_at.tzinfo is not None
    assert await collect(await storage.get(blob.key)) == KNOWN_PAYLOAD
    assert await storage.exists(blob.key) is True
    assert await storage.size(blob.key) == len(KNOWN_PAYLOAD)


async def test_put_reports_the_bytes_it_consumed_and_echoes_the_content_type(
    storage: FilesystemBlobStorage,
) -> None:
    """
    `size` counts what came off the stream, not what any single chunk held.

    """
    blob = await storage.put(chunked(KNOWN_PAYLOAD, 2), content_type="application/json")

    assert blob.size == len(KNOWN_PAYLOAD)
    assert blob.content_type == "application/json"
    assert await storage.size(blob.key) == blob.size


async def test_repeat_put_of_identical_content_writes_the_file_only_once(
    storage: FilesystemBlobStorage, tmp_path: Path
) -> None:
    """A second put of the same bytes is an idempotent no-op, not a rewrite.

    The mtime is read back off the file with a real second of separation between
    the calls, so a rewrite cannot hide behind a coarse filesystem timestamp. The
    scratch directory is checked too: the duplicate's spooled file must be removed
    rather than abandoned.
    """
    first = await storage.put(stream(KNOWN_PAYLOAD))
    target = shard_path(tmp_path, KNOWN_DIGEST)
    written_at = os.stat(target).st_mtime_ns

    time.sleep(MTIME_GUARD_SECONDS)
    second = await storage.put(stream(KNOWN_PAYLOAD))

    assert second.key == first.key
    assert os.stat(target).st_mtime_ns == written_at
    assert second.created_at == first.created_at
    assert second.size == first.size
    assert stored_files(tmp_path) == [target]
    assert scratch_files(tmp_path) == []


async def test_content_lands_at_the_two_level_sharded_path(
    storage: FilesystemBlobStorage, tmp_path: Path
) -> None:
    """
    The layout is part of the contract, so it is asserted as literal path
    components rather than by re-deriving it from the key.

    """
    blob = await storage.put(stream(KNOWN_PAYLOAD))

    target = tmp_path.joinpath(*KNOWN_SHARD_COMPONENTS)

    assert "".join(KNOWN_SHARD_COMPONENTS) == KNOWN_DIGEST
    assert blob.key == f"sha256:{KNOWN_DIGEST}"
    assert target.read_bytes() == KNOWN_PAYLOAD
    assert stored_files(tmp_path) == [target]


async def test_an_unknown_key_is_an_error_for_reads_and_a_false_for_exists(
    storage: FilesystemBlobStorage,
) -> None:
    """Absence is `BlobNotFound` everywhere except `exists()`, which never raises.

    Each `get()` here is awaited inside the `raises` block, which is also what
    pins the eager raise: an implementation written as an async generator would
    return an un-awaitable object and fail with `TypeError` instead.
    """
    key = key_for(b"never stored")

    with pytest.raises(BlobNotFound):
        await storage.get(key)

    with pytest.raises(BlobNotFound):
        await storage.size(key)

    with pytest.raises(BlobNotFound):
        await storage.delete(key)

    assert await storage.exists(key) is False


@pytest.mark.parametrize("key", MALFORMED_KEYS)
async def test_a_malformed_key_is_absence_rather_than_a_crash(
    storage: FilesystemBlobStorage, key: str
) -> None:
    """A key the adapter could never have issued is answered as simply not stored.

    The shape is rejected before any path is built, so a traversal attempt is
    turned away exactly like a typo is, and no method leaks an `IndexError` or an
    `OSError` from a nonsense key.
    """
    with pytest.raises(BlobNotFound):
        await storage.get(key)

    with pytest.raises(BlobNotFound):
        await storage.size(key)

    with pytest.raises(BlobNotFound):
        await storage.delete(key)

    assert await storage.exists(key) is False


async def test_empty_content_is_a_stored_blob_rather_than_an_absence(
    storage: FilesystemBlobStorage, tmp_path: Path
) -> None:
    """
    A zero-byte blob exists and has a size; `size()` raising for absence is what
    keeps `0` from having to mean two different things.

    """
    blob = await storage.put(stream())

    assert blob.key == f"sha256:{EMPTY_DIGEST}"
    assert blob.size == 0
    assert await storage.exists(blob.key) is True
    assert await storage.size(blob.key) == 0
    assert await collect(await storage.get(blob.key)) == b""
    assert stored_files(tmp_path) == [shard_path(tmp_path, EMPTY_DIGEST)]


async def test_a_multi_megabyte_payload_streams_through_intact(
    storage: FilesystemBlobStorage,
) -> None:
    """Many small chunks in, bounded chunks out, the same bytes on both sides.

    The payload is not a multiple of either chunk size, so a streaming loop that
    dropped a ragged final chunk would show up as a short read rather than as a
    silently truncated file.
    """
    payload = os.urandom(LARGE_PAYLOAD_SIZE)

    blob = await storage.put(chunked(payload, UPLOAD_CHUNK_SIZE))
    read_back = [chunk async for chunk in await storage.get(blob.key)]

    assert blob.key == key_for(payload)
    assert blob.size == LARGE_PAYLOAD_SIZE
    assert await storage.size(blob.key) == LARGE_PAYLOAD_SIZE
    assert len(read_back) > 1
    assert b"".join(read_back) == payload


async def test_delete_removes_the_blob_and_a_repeat_delete_raises(
    storage: FilesystemBlobStorage, tmp_path: Path
) -> None:
    """
    Unlike a repository's soft delete, removing a blob twice is an error: the
    second caller is working from a stale key.

    """
    blob = await storage.put(stream(KNOWN_PAYLOAD))

    await storage.delete(blob.key)

    assert await storage.exists(blob.key) is False
    assert stored_files(tmp_path) == []

    with pytest.raises(BlobNotFound):
        await storage.delete(blob.key)

    with pytest.raises(BlobNotFound):
        await storage.get(blob.key)


async def test_a_put_interrupted_before_the_rename_publishes_nothing(
    storage: FilesystemBlobStorage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write killed between the scratch file and the rename must not be visible.

    `os.rename` is the adapter's single publication step, so failing it reproduces
    the worst case: the content is fully written and fully hashed, and dies at the
    instant before it would have become readable. A scratch file may survive under
    `tmp/` for a future GC to collect — what must never survive is a file at the
    content-addressed path, which is why the assertion is on `stored_files()`
    rather than on the whole tree.
    """

    def interrupted(*args: object, **kwargs: object) -> None:
        raise OSError("interrupted before publication")

    monkeypatch.setattr(os, "rename", interrupted)

    with pytest.raises(OSError):
        await storage.put(stream(KNOWN_PAYLOAD))

    monkeypatch.undo()

    key = key_for(KNOWN_PAYLOAD)

    assert not shard_path(tmp_path, KNOWN_DIGEST).exists()
    assert stored_files(tmp_path) == []
    assert await storage.exists(key) is False

    with pytest.raises(BlobNotFound):
        await storage.get(key)

    recovered = await storage.put(stream(KNOWN_PAYLOAD))

    assert recovered.key == key
    assert await collect(await storage.get(key)) == KNOWN_PAYLOAD


async def test_a_source_stream_that_fails_mid_put_leaves_nothing_behind(
    storage: FilesystemBlobStorage, tmp_path: Path
) -> None:
    """
    An upload that dies while its bytes are still being spooled takes its own
    partial scratch file with it, and never reaches a content-addressed path.

    """
    with pytest.raises(RuntimeError):
        await storage.put(failing_stream(b"half an upl", RuntimeError("connection lost")))

    assert stored_files(tmp_path) == []
    assert scratch_files(tmp_path) == []


async def test_a_stray_scratch_file_is_never_readable_as_a_blob(
    storage: FilesystemBlobStorage, tmp_path: Path
) -> None:
    """
    Content that has been spooled but not renamed is not stored content: until it
    lands on its sharded path, every read path must report it absent.

    """
    partial = b"written but never renamed"
    scratch = tmp_path / TMP_DIRECTORY_NAME
    scratch.mkdir()
    (scratch / "stray.part").write_bytes(partial)

    key = key_for(partial)

    assert await storage.exists(key) is False
    assert stored_files(tmp_path) == []

    with pytest.raises(BlobNotFound):
        await storage.get(key)

    with pytest.raises(BlobNotFound):
        await storage.size(key)


async def test_check_health_is_true_for_a_writable_root_and_leaves_no_probe(
    storage: FilesystemBlobStorage, tmp_path: Path
) -> None:
    """
    Health is proven by a real write, so the probe it writes has to be cleaned up
    — leaking one file per check would be its own outage.

    """
    assert await storage.check_health() is True
    assert await storage.check_health() is True
    assert list(tmp_path.iterdir()) == []


async def test_check_health_is_false_for_a_root_that_does_not_exist(tmp_path: Path) -> None:
    """
    A root nothing has created yet is not ready; the adapter reports it rather
    than creating it, since `put()` owns tree creation.

    """
    missing = tmp_path / "does-not-exist"

    assert await FilesystemBlobStorage(missing).check_health() is False
    assert not missing.exists()


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="a read-only directory is not meaningful off POSIX, and root ignores the mode",
)
async def test_check_health_is_false_for_a_root_that_rejects_writes(tmp_path: Path) -> None:
    """
    An existing but unwritable root is down, not up: permission bits are never
    inspected, so only the probe write can tell the difference.

    """
    read_only = tmp_path / "read-only"
    read_only.mkdir()
    read_only.chmod(0o500)

    try:
        assert await FilesystemBlobStorage(read_only).check_health() is False
    finally:
        read_only.chmod(0o700)
