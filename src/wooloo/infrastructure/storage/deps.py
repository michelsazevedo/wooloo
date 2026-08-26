"""
FastAPI providers for the blob storage adapter and its application service.

"""

from typing import Annotated

from fastapi import Depends

from wooloo.application.services.storage_service import StorageService
from wooloo.config.settings import get_settings
from wooloo.domain.storage.contracts import BlobStorage
from wooloo.infrastructure.storage.factory import build_blob_storage


def get_blob_storage() -> BlobStorage:
    """Build the configured blob storage adapter for this request.

    Returns:
        The adapter named by `STORAGE_BACKEND`.

    Raises:
        NotImplementedError: If the configured backend is a recognised name with
            no adapter behind it. Propagated from :func:`build_blob_storage`; it
            surfaces as a `500`, which is the honest answer to a deployment
            configured for a backend this build cannot serve.
    """
    return build_blob_storage(get_settings())


BlobStorageDep = Annotated[BlobStorage, Depends(get_blob_storage)]
"""
The configured blob storage adapter, injected by FastAPI.

"""


def get_storage_service(storage: BlobStorageDep) -> StorageService:
    """Build a :class:`StorageService` bound to the configured adapter.

    Args:
        storage: The adapter supplied by :func:`get_blob_storage`. Annotated as the
            `BlobStorage` protocol, so mypy checks the concrete adapter's
            structural conformance where :func:`build_blob_storage` returns it
            rather than here.

    Returns:
        A service bound to that adapter.
    """
    return StorageService(storage)


StorageServiceDep = Annotated[StorageService, Depends(get_storage_service)]
"""
The blob storage application service, injected by FastAPI.

"""
