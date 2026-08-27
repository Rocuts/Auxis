"""The job path, including the gate's 202-then-'missing credentials'
contract: an upload accepted without usable mapping credentials is a valid
upload against a misconfigured service — the HTTP answer stays 202, and the
truth surfaces on the job, typed, via GET /jobs/{id}.
"""

from __future__ import annotations

import json
import pathlib

import psycopg
import pytest
from fastapi.testclient import TestClient

from tax_tables.service.jobs import DEFAULT_LEASE_SECONDS
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


class TestKilledWorkerReclaim:
    """A worker the platform kills mid-pipeline leaves its row in `running`
    with nobody left to finish it. Measured on production 2026-08-27: five
    jobs stranded that way, and the cron backstop could not see any of them
    because it only ever selected `queued`.

    The fix is a visibility timeout — the same semantics the AWS design
    inherits from Step Functions, and that SQS spells `VisibilityTimeout`.
    """

    @staticmethod
    def _strand(job_id: str, *, age_seconds: int, attempt: int = 1) -> None:
        """Forge exactly what a killed worker leaves behind: status `running`,
        a `started_at` in the past, and no `finished_at`."""
        with psycopg.connect(TEST_DSN) as conn, conn.transaction():
            conn.execute(
                """
                UPDATE jobs
                SET status = 'running',
                    attempt = %s,
                    started_at = now() - make_interval(secs => %s),
                    finished_at = NULL
                WHERE id = %s
                """,
                (attempt, age_seconds, job_id),
            )

    def test_expired_lease_is_reclaimed_and_finished(
        self, client: TestClient, no_credentials: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression this whole class exists for: a stranded job must
        reach a terminal state, not sit in `running` forever."""
        monkeypatch.setenv("JOB_LEASE_SECONDS", "300")
        job_id = client.post(
            "/documents", content=tiny_pdf(text="Killed worker"), headers=AUTH
        ).json()["job_id"]
        self._strand(job_id, age_seconds=600)

        swept = client.post("/internal/sweep", headers=BEARER)
        assert swept.status_code == 200
        assert job_id in swept.json()["processed"]

        reclaimed = client.get(f"/jobs/{job_id}").json()
        assert reclaimed["status"] == "failed"  # terminal, not stranded
        assert reclaimed["finished_at"] is not None
        assert reclaimed["attempt"] == 2  # the reclaim is a second attempt

    def test_live_worker_is_never_stolen(
        self, client: TestClient, no_credentials: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The safety half, and the reason the lease must be >= maxDuration:
        a `running` job still inside its lease belongs to a live worker.
        Reclaiming it would map the same document twice and pay twice."""
        monkeypatch.setenv("JOB_LEASE_SECONDS", "300")
        job_id = client.post(
            "/documents", content=tiny_pdf(text="Still working"), headers=AUTH
        ).json()["job_id"]
        self._strand(job_id, age_seconds=10)  # well inside the lease

        assert client.post("/internal/sweep", headers=BEARER).json()["processed"] == []
        assert client.get(f"/jobs/{job_id}").json()["status"] == "running"

    def test_reclaim_stops_at_max_attempts(
        self, client: TestClient, no_credentials: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A document that reliably kills its worker must not be retried
        forever: every retry spends model credit. It is abandoned to a
        terminal `failed` with a typed reason a human can search for."""
        monkeypatch.setenv("JOB_LEASE_SECONDS", "300")
        monkeypatch.setenv("JOB_MAX_ATTEMPTS", "3")
        job_id = client.post("/documents", content=tiny_pdf(text="Poison"), headers=AUTH).json()[
            "job_id"
        ]
        self._strand(job_id, age_seconds=600, attempt=3)

        assert job_id in client.post("/internal/sweep", headers=BEARER).json()["processed"]
        abandoned = client.get(f"/jobs/{job_id}").json()
        assert abandoned["status"] == "failed"
        assert abandoned["error"]["type"] == "lease_expired_max_attempts"
        assert abandoned["attempt"] == 3  # abandoned, not incremented again

    def test_reclaimed_then_failed_job_accepts_a_fresh_upload(
        self, client: TestClient, no_credentials: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end idempotency after the fix. Production 2026-08-27 could
        not re-seed at all: the stranded jobs read as live, so the sha256
        natural key returned the stranded job instead of starting a new one.
        Once the job reaches a terminal state that unblocks."""
        monkeypatch.setenv("JOB_LEASE_SECONDS", "300")
        body = tiny_pdf(text="Re-seed after strand")
        first = client.post("/documents", content=body, headers=AUTH).json()
        self._strand(first["job_id"], age_seconds=600)

        # While stranded, a re-upload is refused a new job — the bug's shape.
        blocked = client.post("/documents", content=body, headers=AUTH).json()
        assert blocked["job_id"] == first["job_id"]

        client.post("/internal/sweep", headers=BEARER)  # reclaim -> terminal

        retried = client.post("/documents", content=body, headers=AUTH).json()
        assert retried["document_id"] == first["document_id"]
        assert retried["job_id"] != first["job_id"]


class TestLeaseInvariant:
    """The lease is only safe while it is at least as long as the platform's
    `maxDuration`. That couples a constant in Python to a number in
    `vercel.json`, and a coupling nobody checks is a coupling that drifts —
    so it is checked here rather than trusted to a comment.

    Raising `maxDuration` in `vercel.json` without raising the lease would
    silently re-introduce double-processing: a worker still legitimately
    running past the lease would have its job reclaimed by the sweep, and the
    same document would be mapped, verified and billed twice.
    """

    @staticmethod
    def _configured_max_duration() -> int:
        config = json.loads(
            (pathlib.Path(__file__).resolve().parents[2] / "vercel.json").read_text()
        )
        duration = config["functions"]["app/main.py"]["maxDuration"]
        assert isinstance(duration, int)
        return duration

    def test_default_lease_covers_the_configured_max_duration(self) -> None:
        assert self._configured_max_duration() <= DEFAULT_LEASE_SECONDS

    def test_configured_max_duration_is_within_the_plan_ceiling(self) -> None:
        """Vercel's published limit for Pro/Enterprise is 1800 s (extended
        max duration); Hobby caps at 300 s. Asking for more than the plan
        allows is a deploy-time failure, so it is caught here instead."""
        assert self._configured_max_duration() <= 1800

    def test_cron_batch_fits_inside_max_duration(self) -> None:
        """`sweep_pending` processes its batch **sequentially** in one
        invocation, so the cron's `limit` and `maxDuration` are coupled:
        `limit * slowest_document` must fit, with room left for the retry
        backoff measured under fan-out. At `limit=5` against the old 300 s
        budget a single document could not finish, let alone five.
        """
        import urllib.parse

        config = json.loads(
            (pathlib.Path(__file__).resolve().parents[2] / "vercel.json").read_text()
        )
        (cron,) = config["crons"]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(cron["path"]).query)
        limit = int(query["limit"][0])
        slowest_measured_seconds = 346
        assert limit * slowest_measured_seconds <= self._configured_max_duration() * 0.75

    def test_max_duration_covers_the_slowest_measured_document(self) -> None:
        """CLAUDE.md's sizing rule: `maxDuration` is sized to the slowest
        single-document run plus margin. The slowest measured run is document
        03 at 346 s (gate 6, mapper 331.4 s + verifier 15.1 s), and margin
        must now also absorb the 429 backoff measured under fan-out.
        `maxDuration` was 300 s when production stranded five jobs — that
        violated this rule before concurrency ever made it worse.
        """
        slowest_measured_seconds = 346
        assert self._configured_max_duration() >= slowest_measured_seconds * 2
