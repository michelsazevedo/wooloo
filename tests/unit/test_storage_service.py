"""
Unit tests for `StorageService`.

"""

import pytest
from _storage_fakes import FakeBlobStorage, PutCall, byte_stream, make_stored_blob
from structlog.typing import EventDict

from wooloo.application.services.storage_service import StorageService
from wooloo.domain.storage.exceptions import BlobNotFound

CONTENT_CHUNKS = (b"woo", b"loo")
"""The upload body, deliberately arriving in more than one chunk."""

CONTENT = b"".join(CONTENT_CHUNKS)
"""The same body as the port must observe it: every chunk, in order."""

CONTENT_TYPE = "application/vnd.oci.image.layer.v1.tar"
"""The media type a caller declares on upload."""

STORED_KEY = f"sha256:{'ab' * 32}"
"""The key the backend assigns, shaped like a real one but never derived here."""

STORED_SIZE = 4096
"""
The byte count the backend reports on the blob it stored.

Deliberately not `len(CONTENT)`. The service logs fields taken from the returned
blob, not from the stream it forwarded, and those two are indistinguishable in a
test whose double echoes the length it was given. Making them disagree is the only
reason an implementation that logged the bytes it streamed could ever fail here.
"""

MEASURED_SIZE = 512
"""
The byte count `size()` reports for a stored key.

Distinct from `STORED_SIZE` so a read path that logged a write path's number — or
a constant — is visible rather than accidentally correct.
"""

MISSING_KEY = f"sha256:{'cd' * 32}"
"""A well-formed key the backend does not know about."""


def events_named(events: list[EventDict], name: str) -> list[EventDict]:
    """Select one kind of event from everything captured during a test.

    Args:
        events: Every structlog event the test captured, in order.
        name: The event name to keep.

    Returns:
        Only the matching events, in order.
    """
    return [event for event in events if event["event"] == name]


async def test_store_forwards_the_content_and_content_type_to_the_port() -> None:
    """
    The port receives every uploaded byte, in order, with the declared type.

    """
    storage = FakeBlobStorage(put_returns=make_stored_blob(STORED_KEY, size=STORED_SIZE))

    await StorageService(storage).store(byte_stream(*CONTENT_CHUNKS), content_type=CONTENT_TYPE)

    assert storage.put_calls == [PutCall(content=CONTENT, content_type=CONTENT_TYPE)]


async def test_store_returns_the_ports_blob() -> None:
    """
    The caller gets the port's own blob, not a reconstruction of it.

    """
    stored = make_stored_blob(STORED_KEY, size=STORED_SIZE, content_type=CONTENT_TYPE)
    storage = FakeBlobStorage(put_returns=stored)

    result = await StorageService(storage).store(byte_stream(*CONTENT_CHUNKS))

    assert result is stored


async def test_store_logs_the_stored_blob_rather_than_the_input(
    captured_logs: list[EventDict],
) -> None:
    """
    `blob_stored` reports the blob that was written, not what was sent.

    """
    storage = FakeBlobStorage(put_returns=make_stored_blob(STORED_KEY, size=STORED_SIZE))

    await StorageService(storage).store(byte_stream(*CONTENT_CHUNKS), content_type=CONTENT_TYPE)

    stored = events_named(captured_logs, "blob_stored")
    assert len(stored) == 1
    assert stored[0]["key"] == STORED_KEY
    assert stored[0]["size"] == STORED_SIZE
    assert stored[0]["size"] != len(CONTENT)
    assert stored[0]["log_level"] == "info"


async def test_retrieve_returns_the_stream_the_port_opened() -> None:
    """The caller reads the port's own iterator, chunk boundaries and all.

    Identity, not equality: wrapping the stream in a re-chunking or buffering
    layer here would defeat the streaming contract the adapter exists to honour,
    while still comparing equal on the bytes it eventually yields.
    """
    stream = byte_stream(*CONTENT_CHUNKS)
    storage = FakeBlobStorage(get_returns=stream, size_returns=MEASURED_SIZE)

    result = await StorageService(storage).retrieve(STORED_KEY)

    assert result is stream
    assert storage.get_calls == [STORED_KEY]


async def test_retrieve_logs_the_key_and_the_measured_size(
    captured_logs: list[EventDict],
) -> None:
    """`blob_retrieved` carries the requested key and the port's byte count."""
    storage = FakeBlobStorage(get_returns=byte_stream(*CONTENT_CHUNKS), size_returns=MEASURED_SIZE)

    await StorageService(storage).retrieve(STORED_KEY)

    retrieved = events_named(captured_logs, "blob_retrieved")
    assert len(retrieved) == 1
    assert retrieved[0]["key"] == STORED_KEY
    assert retrieved[0]["size"] == MEASURED_SIZE
    assert retrieved[0]["log_level"] == "info"
    assert storage.size_calls == [STORED_KEY]


async def test_retrieve_propagates_blob_not_found_without_logging(
    captured_logs: list[EventDict],
) -> None:
    """
    A stale key reaches the caller as the port's own exception, unrecorded.

    """
    missing = BlobNotFound(f"unknown key: {MISSING_KEY!r}")
    storage = FakeBlobStorage(size_raises=missing)

    with pytest.raises(BlobNotFound) as raised:
        await StorageService(storage).retrieve(MISSING_KEY)

    assert raised.value is missing
    assert storage.size_calls == [MISSING_KEY]
    assert storage.get_calls == []
    assert captured_logs == []


async def test_remove_deletes_the_key_and_logs_its_pre_delete_size(
    captured_logs: list[EventDict],
) -> None:
    """
    A removal reaches the port and is recorded with the size it had.

    """
    storage = FakeBlobStorage(size_returns=MEASURED_SIZE, delete_succeeds=True)

    await StorageService(storage).remove(STORED_KEY)

    assert storage.size_calls == [STORED_KEY]
    assert storage.delete_calls == [STORED_KEY]

    deleted = events_named(captured_logs, "blob_deleted")
    assert len(deleted) == 1
    assert deleted[0]["key"] == STORED_KEY
    assert deleted[0]["size"] == MEASURED_SIZE
    assert deleted[0]["log_level"] == "info"


async def test_remove_propagates_blob_not_found_without_deleting(
    captured_logs: list[EventDict],
) -> None:
    """
    An unknown key is rejected before anything is removed or recorded.

    """
    storage = FakeBlobStorage()

    with pytest.raises(BlobNotFound):
        await StorageService(storage).remove(MISSING_KEY)

    assert storage.size_calls == [MISSING_KEY]
    assert storage.delete_calls == []
    assert captured_logs == []
