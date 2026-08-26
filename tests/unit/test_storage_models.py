"""
Unit tests for the `StoredBlob` storage value object.

"""

from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime
from typing import Final

import pytest

from wooloo.domain.storage.models import StoredBlob

FIELD_NAMES: Final = tuple(field.name for field in fields(StoredBlob))
"""
The blob's fields, read off the class rather than hard-coded.

"""

SAMPLE_KEY: Final = f"sha256:{'ab' * 32}"

SAMPLE_CONTENT_TYPE: Final = "application/vnd.oci.image.layer.v1.tar+gzip"

SAMPLE_CREATED_AT: Final = datetime(2026, 5, 14, 11, 20, tzinfo=UTC)

REPLACEMENTS: Final = {
    "key": f"sha256:{'cd' * 32}",
    "size": 999,
    "content_type": "text/plain",
    "created_at": datetime(1999, 1, 1, tzinfo=UTC),
}
"""A plausible-but-wrong value per field, used to attempt mutation.

Each differs from the sample blob's value, so a mutation that *succeeded* would
be observable rather than a no-op assignment that only looks frozen.
"""


def make_blob(content_type: str | None = SAMPLE_CONTENT_TYPE) -> StoredBlob:
    """Build a fully-populated blob.

    Args:
        content_type: Media type recorded at write time, or `None` for a caller
            that supplied none.

    Returns:
        A fresh instance; the value object is frozen, so no state leaks between
        tests.
    """
    return StoredBlob(
        key=SAMPLE_KEY,
        size=4096,
        content_type=content_type,
        created_at=SAMPLE_CREATED_AT,
    )


def test_the_blob_carries_exactly_the_four_documented_fields() -> None:
    """
    Pins the field set the mutation tests iterate over.

    """
    assert FIELD_NAMES == ("key", "size", "content_type", "created_at")


@pytest.mark.parametrize("field_name", FIELD_NAMES)
def test_no_field_can_be_reassigned(field_name: str) -> None:
    """
    Every field is frozen, checked one field at a time.

    """
    blob = make_blob()
    original = getattr(blob, field_name)
    replacement = REPLACEMENTS[field_name]
    assert replacement != original

    with pytest.raises(FrozenInstanceError):
        setattr(blob, field_name, replacement)

    assert getattr(blob, field_name) == original


@pytest.mark.parametrize("field_name", FIELD_NAMES)
def test_no_field_can_be_deleted(field_name: str) -> None:
    """
    Deletion is blocked as well as assignment: a frozen dataclass guards
    `__delattr__` too, where a hand-rolled shim guarding only `__setattr__` would
    leave a blob that can be hollowed out into an unusable half-object.

    """
    blob = make_blob()

    with pytest.raises(FrozenInstanceError):
        delattr(blob, field_name)

    assert hasattr(blob, field_name)


def test_new_attributes_cannot_be_attached() -> None:
    """
    Immutability covers unknown names too, so no ad-hoc state — a filesystem path,
    a bucket name — can be smuggled onto a backend-agnostic value object.

    """
    blob = make_blob()

    with pytest.raises(FrozenInstanceError):
        blob.storage_path = "/tmp/wooloo/ab/12"  # type: ignore[attr-defined]

    assert not hasattr(blob, "storage_path")


def test_a_blob_constructs_without_a_content_type() -> None:
    """`content_type=None` is the normal case for a caller that supplied none.

    It is stored as `None` rather than coerced to a placeholder string, which is
    what lets the download path decide for itself what to serve.
    """
    blob = make_blob(content_type=None)

    assert blob.key == SAMPLE_KEY
    assert blob.size == 4096
    assert blob.content_type is None
    assert blob.created_at == SAMPLE_CREATED_AT


def test_a_blob_constructs_with_a_content_type() -> None:
    """
    A supplied media type is echoed back verbatim — not parsed, normalised, or
    validated — since this is the one and only call that ever reports it.

    """
    blob = make_blob()

    assert blob.key == SAMPLE_KEY
    assert blob.size == 4096
    assert blob.content_type == SAMPLE_CONTENT_TYPE
    assert blob.created_at == SAMPLE_CREATED_AT
