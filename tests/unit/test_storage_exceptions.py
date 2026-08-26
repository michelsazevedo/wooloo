"""
Unit tests for the storage domain exception hierarchy.

"""

from typing import Final

import pytest

from wooloo.domain.storage.exceptions import (
    BlobAlreadyExists,
    BlobNotFound,
    StorageException,
)

EXCEPTION_TYPES: Final = (StorageException, BlobNotFound, BlobAlreadyExists)

EXCEPTION_IDS: Final = [exc_type.__name__ for exc_type in EXCEPTION_TYPES]

SAMPLE_MESSAGE: Final = "unknown key: 'sha256:deadbeef'"


@pytest.mark.parametrize("exc_type", EXCEPTION_TYPES, ids=EXCEPTION_IDS)
def test_an_omitted_message_reads_as_an_empty_string(
    exc_type: type[StorageException],
) -> None:
    """
    Nothing is forwarded to `Exception` when no message is given.

    """
    for exc in (exc_type(), exc_type(None)):
        assert exc.message is None
        assert str(exc) == ""
        assert exc.args == ()


@pytest.mark.parametrize("exc_type", EXCEPTION_TYPES, ids=EXCEPTION_IDS)
def test_a_supplied_message_is_forwarded_and_kept(
    exc_type: type[StorageException],
) -> None:
    """
    A message reaches both `Exception` and the `message` attribute.

    """
    exc = exc_type(SAMPLE_MESSAGE)

    assert exc.message == SAMPLE_MESSAGE
    assert str(exc) == SAMPLE_MESSAGE
    assert exc.args == (SAMPLE_MESSAGE,)


@pytest.mark.parametrize("exc_type", EXCEPTION_TYPES, ids=EXCEPTION_IDS)
def test_every_storage_failure_is_catchable_through_the_base(
    exc_type: type[StorageException],
) -> None:
    """
    A single `except StorageException` catches the whole family, which is what
    lets the API layer install one storage-error boundary instead of enumerating
    leaf types it would have to remember to extend.

    """
    assert issubclass(exc_type, StorageException)

    with pytest.raises(StorageException):
        raise exc_type(SAMPLE_MESSAGE)


def test_the_two_leaf_types_are_siblings_rather_than_ancestors() -> None:
    """
    Neither leaf may absorb the other.

    """
    assert not issubclass(BlobNotFound, BlobAlreadyExists)
    assert not issubclass(BlobAlreadyExists, BlobNotFound)

    with pytest.raises(BlobNotFound):
        try:
            raise BlobNotFound(SAMPLE_MESSAGE)
        except BlobAlreadyExists as exc:
            pytest.fail(f"BlobNotFound was caught as BlobAlreadyExists: {exc!r}")
