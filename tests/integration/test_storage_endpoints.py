"""
Integration tests for the temporary storage HTTP surface.

"""

import hashlib
from pathlib import Path
from typing import Final

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from wooloo.domain.storage.contracts import BlobStorage
from wooloo.infrastructure.storage.deps import get_blob_storage
from wooloo.infrastructure.storage.filesystem import FilesystemBlobStorage
from wooloo.main import app, asgi_app

BLOBS_URL: Final = "/api/v1/storage/blobs"

PAYLOAD: Final = b"wooloo temporary blob payload"

ERROR_FIELDS: Final = {"code", "message", "request_id"}
"""
Exactly what `ErrorResponse` exposes, asserted as set equality so a leaked field
fails here too.

"""

UNKNOWN_KEY: Final = f"sha256:{'0' * 64}"
"""
A well-formed key nothing was ever stored under.

Well-formed on purpose: a malformed key would be rejected by the adapter's key
parsing and could return `404` without the lookup ever running.

"""


@pytest.fixture
def storage_root(tmp_path: Path) -> Path:
    """Give one test a private, empty storage tree.

    Args:
        tmp_path: pytest's per-test temporary directory.

    Returns:
        An existing, empty directory for the adapter to write under.
    """
    root = tmp_path / "blobs"
    root.mkdir()

    return root


@pytest.fixture
def client(storage_root: Path) -> TestClient:
    """Return a client for the served application, wired to this test's tree.

    Args:
        storage_root: The directory blobs must land under.

    Returns:
        A client driving `asgi_app`, middleware included. Not entered as a context
        manager, so the application's lifespan — which builds a real engine and
        reconfigures logging — never runs. The annotation on `storage` is what
        makes the adapter's conformance to the `BlobStorage` protocol a
        type-checked fact at this call site.
    """
    storage: BlobStorage = FilesystemBlobStorage(storage_root)
    app.dependency_overrides[get_blob_storage] = lambda: storage

    return TestClient(asgi_app)


def upload(client: TestClient, payload: bytes = PAYLOAD) -> Response:
    """Upload one payload as a multipart file part.

    Args:
        client: The client to issue the request through.
        payload: The bytes to send.

    Returns:
        The raw response, so callers can assert on the failure cases too.
    """
    return client.post(
        BLOBS_URL,
        files={"file": ("layer.bin", payload, "application/octet-stream")},
    )


def upload_key(client: TestClient, payload: bytes = PAYLOAD) -> str:
    """Upload one payload, assert it worked, and return its key.

    Args:
        client: The client to issue the request through.
        payload: The bytes to send.

    Returns:
        The key the server assigned.
    """
    response = upload(client, payload)
    assert response.status_code == 201, response.text

    key: str = response.json()["key"]

    return key


def sharded_path(storage_root: Path, payload: bytes) -> Path:
    """Derive where a payload's bytes must be found on disk.

    Args:
        storage_root: The tree the adapter was pointed at.
        payload: The uploaded bytes.

    Returns:
        `<storage_root>/<hex[:2]>/<hex[2:4]>/<hex[4:]>`.
    """
    digest = hashlib.sha256(payload).hexdigest()

    return storage_root / digest[:2] / digest[2:4] / digest[4:]


# ---------------------------------------------------------------------------
# POST /api/v1/storage/blobs
# ---------------------------------------------------------------------------


def test_uploading_a_blob_returns_201_with_the_content_derived_key(
    client: TestClient,
) -> None:
    """
    The key must be the payload's own sha256, and the size its real byte count.

    """
    response = upload(client)

    assert response.status_code == 201
    assert response.json() == {
        "key": f"sha256:{hashlib.sha256(PAYLOAD).hexdigest()}",
        "size": len(PAYLOAD),
    }


def test_uploading_a_blob_actually_writes_it_to_the_sharded_path(
    client: TestClient, storage_root: Path
) -> None:
    """A `201` must mean the bytes are on disk, at the layout the adapter promises.

    Asserted against the filesystem rather than against a follow-up `GET`, which
    would pass just as well if the content only ever lived in memory.
    """
    assert upload(client).status_code == 201

    path = sharded_path(storage_root, PAYLOAD)
    assert path.is_file()
    assert path.read_bytes() == PAYLOAD


# ---------------------------------------------------------------------------
# GET /api/v1/storage/blobs/{key}
# ---------------------------------------------------------------------------


def test_downloading_a_blob_returns_the_exact_bytes_that_were_uploaded(
    client: TestClient,
) -> None:
    """
    A round trip through the storage layer must not alter a single byte.

    """
    key = upload_key(client)

    response = client.get(f"{BLOBS_URL}/{key}")

    assert response.status_code == 200
    assert response.content == PAYLOAD
    assert response.headers["content-type"] == "application/octet-stream"


def test_downloading_an_unknown_key_returns_404_not_found(client: TestClient) -> None:
    """
    An unknown key must be a clean `404`, not a truncated `200`.

    """
    response = client.get(f"{BLOBS_URL}/{UNKNOWN_KEY}")

    assert response.status_code == 404
    body = response.json()
    assert set(body) == ERROR_FIELDS
    assert body["code"] == "not_found"
    assert body["message"]
    assert body["request_id"] == response.headers["x-request-id"]


# ---------------------------------------------------------------------------
# DELETE /api/v1/storage/blobs/{key}
# ---------------------------------------------------------------------------


def test_deleting_a_blob_returns_204_and_removes_it_from_disk(
    client: TestClient, storage_root: Path
) -> None:
    """A successful delete must return no content and leave no file behind."""
    key = upload_key(client)

    response = client.delete(f"{BLOBS_URL}/{key}")

    assert response.status_code == 204
    assert response.text == ""
    assert "content-type" not in response.headers
    assert not sharded_path(storage_root, PAYLOAD).exists()


def test_a_deleted_blob_is_gone_and_a_repeat_delete_is_a_404(client: TestClient) -> None:
    """
    Blob deletion is deliberately *not* idempotent, unlike repository deletion.

    """
    key = upload_key(client)

    assert client.delete(f"{BLOBS_URL}/{key}").status_code == 204
    assert client.get(f"{BLOBS_URL}/{key}").status_code == 404

    second = client.delete(f"{BLOBS_URL}/{key}")

    assert second.status_code == 404
    assert second.json()["code"] == "not_found"


def test_deleting_an_unknown_key_returns_404_not_found(client: TestClient) -> None:
    """
    A key that was never issued must be refused in the same terms as a stale one.

    """
    response = client.delete(f"{BLOBS_URL}/{UNKNOWN_KEY}")

    assert response.status_code == 404
    assert set(response.json()) == ERROR_FIELDS
    assert response.json()["code"] == "not_found"


# ---------------------------------------------------------------------------
# Middleware contract
# ---------------------------------------------------------------------------


def test_every_storage_response_carries_the_middleware_headers(client: TestClient) -> None:
    """
    The middleware contract must hold on this surface too, bodies and all.

    """
    created = upload(client)
    key = created.json()["key"]

    downloaded = client.get(f"{BLOBS_URL}/{key}")
    deleted = client.delete(f"{BLOBS_URL}/{key}")
    missing = client.get(f"{BLOBS_URL}/{key}")

    responses = (created, downloaded, deleted, missing)
    assert [response.status_code for response in responses] == [201, 200, 204, 404]

    for response in responses:
        assert response.headers["x-request-id"]
        assert response.headers["x-content-type-options"] == "nosniff"

    assert len({response.headers["x-request-id"] for response in responses}) == len(responses)
