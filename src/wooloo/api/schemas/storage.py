"""
Request and response bodies for the storage endpoints.

"""

from pydantic import BaseModel


class BlobUploadResponse(BaseModel):
    """Body of ``POST /api/v1/storage/blobs``.

    Attributes:
        key: The storage key identifying the newly stored blob, as returned by
            the storage layer. Its format is backend-defined and opaque to API
            consumers, who should round-trip it verbatim when fetching or
            deleting the blob.
        size: Size of the stored blob in bytes.
    """

    key: str

    size: int
