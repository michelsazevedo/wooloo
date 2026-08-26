"""
A `BlobStorage` double for the modules that exercise `/api/v1/healthz`.

"""

from collections.abc import AsyncIterator

from wooloo.domain.storage.contracts import BlobStorage
from wooloo.domain.storage.models import StoredBlob
from wooloo.infrastructure.storage.deps import get_blob_storage
from wooloo.main import app


class HealthOnlyBlobStorage:
    """A `BlobStorage` whose readiness verdict is whatever the test configured.

    Every other method raises. The health endpoint must reach the backend through
    `check_health()` alone, so a probe that grew into reading or writing real
    blobs fails loudly here instead of passing quietly against a temp directory.
    """

    def __init__(self, *, healthy: bool) -> None:
        """Configure the double's answer.

        Args:
            healthy: What `check_health()` reports, for every request in the test.
        """
        self._healthy = healthy

    async def put(
        self,
        content: AsyncIterator[bytes],
        *,
        content_type: str | None = None,
    ) -> StoredBlob:
        """Fail: a health probe must never write a blob."""
        raise AssertionError("put() is not part of the health probe")

    async def get(self, key: str) -> AsyncIterator[bytes]:
        """Fail: a health probe must never read a blob."""
        raise AssertionError("get() is not part of the health probe")

    async def exists(self, key: str) -> bool:
        """Fail: a health probe must never look a blob up."""
        raise AssertionError("exists() is not part of the health probe")

    async def size(self, key: str) -> int:
        """Fail: a health probe must never measure a blob."""
        raise AssertionError("size() is not part of the health probe")

    async def delete(self, key: str) -> None:
        """Fail: a health probe must never remove a blob."""
        raise AssertionError("delete() is not part of the health probe")

    async def check_health(self) -> bool:
        """Report the configured verdict.

        Returns:
            `True` or `False`, as configured.
        """
        return self._healthy


def override_blob_storage(*, healthy: bool = True) -> None:
    """Pin storage's readiness for every request the test makes.

    Args:
        healthy: The verdict `/api/v1/healthz` must report storage under. The
            annotation below is what makes the double's conformance to the
            `BlobStorage` protocol a type-checked fact rather than a hope.
    """
    storage: BlobStorage = HealthOnlyBlobStorage(healthy=healthy)

    app.dependency_overrides[get_blob_storage] = lambda: storage
