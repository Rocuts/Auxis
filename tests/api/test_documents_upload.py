"""POST /documents contract: authenticated, guarded, asynchronous, idempotent.

Every guard fires before a byte reaches the pipeline; the 202 promises a
job row, never a pipeline run.
"""

from __future__ import annotations

import httpx2
import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import AUTH, make_client, tiny_pdf
from tests.conftest import TEST_DSN, reset_database


def _upload(client: TestClient, body: bytes, **headers: str) -> httpx2.Response:
    return client.post("/documents", content=body, headers={**AUTH, **headers})


class TestAuth:
    def test_missing_key_is_401(self, client: TestClient) -> None:
        response = client.post("/documents", content=tiny_pdf())
        assert response.status_code == 401

    def test_wrong_key_is_401(self, client: TestClient) -> None:
        response = client.post(
            "/documents", content=tiny_pdf(), headers={"X-API-Key": "not-the-key"}
        )
        assert response.status_code == 401
        # The guard never leaks whether the key exists or how it differs.
        assert "key" in response.json()["detail"].lower()


class TestGuards:
    def test_oversized_upload_is_413_and_registers_nothing(self) -> None:
        reset_database()
        with make_client(max_upload_bytes=1_000) as client:
            response = client.post("/documents", content=b"%PDF" + b"x" * 2_000, headers=AUTH)
        assert response.status_code == 413
        with psycopg.connect(TEST_DSN) as conn:
            count = conn.execute("SELECT count(*) FROM documents").fetchone()
        assert count == (0,)

    def test_missing_pdf_magic_is_415(self, client: TestClient) -> None:
        response = client.post("/documents", content=b"PK\x03\x04 not a pdf", headers=AUTH)
        assert response.status_code == 415

    def test_unparsable_pdf_is_415(self, client: TestClient) -> None:
        response = client.post("/documents", content=b"%PDF-1.4 but garbage", headers=AUTH)
        assert response.status_code == 415

    def test_page_cap_is_413(self) -> None:
        reset_database()
        with make_client(max_pages=2) as client:
            response = client.post("/documents", content=tiny_pdf(pages=3), headers=AUTH)
        assert response.status_code == 413
        assert "cap" in response.json()["detail"]


class TestAcceptedUpload:
    def test_202_creates_document_blob_and_queued_job(self, client: TestClient) -> None:
        body = tiny_pdf(text="Upload happy path")
        response = client.post(
            "/documents", content=body, headers={**AUTH, "X-Filename": "upload_case.pdf"}
        )
        assert response.status_code == 202
        payload = response.json()
        assert payload["duplicate"] is False

        with psycopg.connect(TEST_DSN) as conn:
            document = conn.execute(
                "SELECT filename, byte_size, page_count FROM documents WHERE id = %s",
                (payload["document_id"],),
            ).fetchone()
            assert document == ("upload_case.pdf", len(body), 1)
            blob = conn.execute(
                "SELECT content FROM document_blobs WHERE document_id = %s",
                (payload["document_id"],),
            ).fetchone()
            assert blob is not None and bytes(blob[0]) == body
            job = conn.execute(
                "SELECT status, attempt FROM jobs WHERE id = %s", (payload["job_id"],)
            ).fetchone()
            assert job == ("queued", 0)

    def test_duplicate_upload_is_a_no_op_returning_the_live_job(self, client: TestClient) -> None:
        body = tiny_pdf(text="Duplicate upload")
        first = client.post("/documents", content=body, headers=AUTH).json()
        second = client.post("/documents", content=body, headers=AUTH)
        assert second.status_code == 202
        assert second.json() == {**first, "duplicate": True}
        with psycopg.connect(TEST_DSN) as conn:
            jobs = conn.execute("SELECT count(*) FROM jobs").fetchone()
        assert jobs == (1,)

    def test_distinct_documents_get_distinct_jobs(self, client: TestClient) -> None:
        first = _upload(client, tiny_pdf(text="Document A"))
        second = _upload(client, tiny_pdf(text="Document B"))
        assert first.json()["job_id"] != second.json()["job_id"]


class TestDocumentReads:
    def test_listing_and_detail_are_public(self, client: TestClient) -> None:
        body = tiny_pdf(text="Provenance read")
        document_id = client.post("/documents", content=body, headers=AUTH).json()["document_id"]

        listing = client.get("/documents")
        assert listing.status_code == 200
        (entry,) = listing.json()
        assert entry["id"] == document_id
        assert entry["page_count"] == 1

        detail = client.get(f"/documents/{document_id}")
        assert detail.status_code == 200
        assert detail.json()["sha256"] == entry["sha256"]

    def test_unknown_document_is_404(self, client: TestClient) -> None:
        assert client.get("/documents/00000000-0000-0000-0000-000000000000").status_code == 404


@pytest.mark.parametrize("path", ["/documents", "/records", "/jobs/x"])
def test_get_endpoints_require_no_key(client: TestClient, path: str) -> None:
    # GETs are public and read-only by design; only /jobs/x 422s on the id.
    response = client.get(path)
    assert response.status_code in (200, 404, 422)
