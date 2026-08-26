"""
Temporary HTTP routes for exercising the storage layer.

"""

from collections.abc import AsyncIterator
from typing import Annotated, Final

from fastapi import APIRouter, File, Response, UploadFile
from fastapi.responses import StreamingResponse

from wooloo.api.schemas.storage import BlobUploadResponse
from wooloo.infrastructure.storage.deps import StorageServiceDep

router = APIRouter(tags=["storage"])

_UPLOAD_CHUNK_SIZE: Final = 64 * 1024

_DOWNLOAD_MEDIA_TYPE: Final = "application/octet-stream"


async def _stream_upload(file: UploadFile) -> AsyncIterator[bytes]:
    """Re-expose an uploaded file as the byte stream the storage port consumes.

    Args:
        file: The uploaded file, positioned at its start.

    Yields:
        Successive chunks of at most `_UPLOAD_CHUNK_SIZE` bytes, in order.
    """
    while chunk := await file.read(_UPLOAD_CHUNK_SIZE):
        yield chunk


@router.post("/blobs", status_code=201, summary="Upload a blob (temporary)")
async def upload_blob(
    storage_service: StorageServiceDep,
    file: Annotated[UploadFile, File()],
) -> BlobUploadResponse:
    """Store an uploaded file and report the key it landed on.

    Args:
        storage_service: The injected storage service.
        file: The `multipart/form-data` part holding the content. Its declared
            `content_type` is passed through as sent — it is echoed back on this
            call's `StoredBlob` and never persisted, so trusting the client with
            it costs nothing.

    Returns:
        The stored blob's key and byte count, under `201 Created`. Re-uploading
        identical bytes returns the same key and the same `201`, because the key
        is derived from the content: there is no second resource to have
        conflicted with.
    """
    blob = await storage_service.store(_stream_upload(file), content_type=file.content_type)

    return BlobUploadResponse(key=blob.key, size=blob.size)


@router.get("/blobs/{key}", summary="Download a blob (temporary)")
async def download_blob(key: str, storage_service: StorageServiceDep) -> StreamingResponse:
    """Stream back the content stored under `key`.

    Args:
        key: A key previously returned by the upload endpoint.
        storage_service: The injected storage service.

    Returns:
        A `200` streaming the content as `application/octet-stream`. The stored
        media type is not served back: the storage layer never persisted it (see
        the PRD's Reconciliation §5), so declaring anything more specific would be
        a guess.

    Raises:
        BlobNotFound: If `key` is unknown. Deliberately not caught here — the
            service raises it while this handler is still awaiting, before
            `StreamingResponse` exists, so the registered handler can answer a
            clean `404` instead of a truncated `200`.
    """
    stream = await storage_service.retrieve(key)

    return StreamingResponse(stream, media_type=_DOWNLOAD_MEDIA_TYPE)


@router.delete(
    "/blobs/{key}",
    status_code=204,
    response_class=Response,
    summary="Delete a blob (temporary)",
)
async def delete_blob(key: str, storage_service: StorageServiceDep) -> None:
    """Delete the content stored under `key`.

    Args:
        key: A key previously returned by the upload endpoint.
        storage_service: The injected storage service.

    Returns:
        `None`, serialised as an empty `204 No Content` body.

    Raises:
        BlobNotFound: If `key` is unknown, including on a repeat delete — unlike
            the repository delete, this one is not idempotent, because a blob is
            physical content rather than a row carrying a soft-delete flag.
            Answered `404` by its handler.
    """
    await storage_service.remove(key)
