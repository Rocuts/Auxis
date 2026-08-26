"""The job path, including the gate's 202-then-'missing credentials'
contract: an upload accepted without usable mapping credentials is a valid
upload against a misconfigured service — the HTTP answer stays 202, and the
truth surfaces on the job, typed, via GET /jobs/{id}.
"""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import AUTH, BEARER, tiny_pdf
from tests.conftest import TEST_DSN

#: Every env var that could supply the semantic layer with a credential.
_CREDENTIAL_VARS = (
    "ANTHROPIC_API_KEY",
    "SCHEMA_MAPPER_API_KEY",
    "RECORD_VERIFIER_API_KEY",
    "ADJUDICATOR_API_KEY",
)


@pytest.fixture()
def no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """The missing-credentials path must be deterministic regardless of the
    developer's shell: strip every credential the configs would find."""
    for name in _CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)


class TestSweepAuth:
    def test_missing_bearer_is_401(self, client: TestClient) -> None:
        assert client.post("/internal/sweep").status_code == 401

    def test_wrong_bearer_is_401(self, client: TestClient) -> None:
        response = client.post(
            "/internal/sweep", headers={"Authorization": "Bearer not-the-secret"}
        )
        assert response.status_code == 401


class TestJobContract:
    def test_unknown_job_is_404(self, client: TestClient) -> None:
        assert client.get("/jobs/00000000-0000-0000-0000-000000000000").status_code == 404

    def test_202_then_missing_credentials(self, client: TestClient, no_credentials: None) -> None:
        accepted = client.post("/documents", content=tiny_pdf(text="Job path"), headers=AUTH)
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]

        # Nothing ran at accept time: the job is queued, untouched.
        queued = client.get(f"/jobs/{job_id}").json()
        assert queued["status"] == "queued"
        assert queued["attempt"] == 0
        assert queued["error"] is None

        swept = client.post("/internal/sweep", headers=BEARER)
        assert swept.status_code == 200
        assert job_id in swept.json()["processed"]

        failed = client.get(f"/jobs/{job_id}").json()
        assert failed["status"] == "failed"
        assert failed["attempt"] == 1
        assert failed["error"]["type"] == "missing_credentials"
        # The error names env variables, never values (anti-goal #10).
        assert "SCHEMA_MAPPER_API_KEY" in failed["error"]["message"]
        assert failed["started_at"] is not None
        assert failed["finished_at"] is not None
        assert failed["records_extracted"] is None

    def test_sweep_is_idempotent_over_terminal_jobs(
        self, client: TestClient, no_credentials: None
    ) -> None:
        job_id = client.post(
            "/documents", content=tiny_pdf(text="Sweep once"), headers=AUTH
        ).json()["job_id"]
        first = client.post("/internal/sweep", headers=BEARER).json()["processed"]
        second = client.post("/internal/sweep", headers=BEARER).json()["processed"]
        assert job_id in first
        assert second == []  # failed is terminal; the sweep never re-claims it

    def test_failed_job_can_be_requeued_by_reupload(
        self, client: TestClient, no_credentials: None
    ) -> None:
        """Re-uploading after an outage is the retry path: a document whose
        latest job FAILED gets a fresh job (unlike a succeeded one)."""
        body = tiny_pdf(text="Retry path")
        first = client.post("/documents", content=body, headers=AUTH).json()
        client.post("/internal/sweep", headers=BEARER)
        retried = client.post("/documents", content=body, headers=AUTH).json()
        assert retried["document_id"] == first["document_id"]
        assert retried["job_id"] != first["job_id"]
        with psycopg.connect(TEST_DSN) as conn:
            statuses = conn.execute("SELECT status FROM jobs ORDER BY created_at").fetchall()
        assert statuses == [("failed",), ("queued",)]


class TestCronSweepEntrypoint:
    """Vercel Cron issues GET, not POST. The mutating GET is the platform's
    contract, not a design choice — but the auth is identical on both."""

    def test_get_is_accepted_with_the_bearer(self, client: TestClient) -> None:
        response = client.get("/internal/sweep", headers=BEARER)
        assert response.status_code == 200
        assert response.json() == {"processed": []}

    def test_get_without_the_bearer_is_401(self, client: TestClient) -> None:
        assert client.get("/internal/sweep").status_code == 401

    def test_get_with_the_wrong_bearer_is_401(self, client: TestClient) -> None:
        assert (
            client.get("/internal/sweep", headers={"Authorization": "Bearer nope"}).status_code
            == 401
        )
