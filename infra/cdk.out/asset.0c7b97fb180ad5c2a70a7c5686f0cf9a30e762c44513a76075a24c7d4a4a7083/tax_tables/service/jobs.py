"""Job lifecycle: enqueue on upload, process by sweep or worker.

``POST /documents`` calls :func:`enqueue_ingest` and returns 202 — the
request never blocks on extraction. Processing happens elsewhere:
:func:`sweep_pending` claims ``queued`` jobs with ``FOR UPDATE SKIP
LOCKED`` (safe under concurrent sweepers) and runs each through
:func:`process_job`, which is where the pipeline's adapters are built from
the environment.

Failure honesty (the contract the 202 owes its callers): a job accepted
without usable mapping credentials is NOT an HTTP error — the upload is
valid, the service is misconfigured — so the job itself fails with
``error.type == "missing_credentials"``, visible on ``GET /jobs/{id}``.
Error payloads carry exception class names and messages that name env
variables, never values (anti-goal #10).

Enqueue idempotency, on top of the document-level sha256 no-op:

- a live (queued/running) job for the document is returned as-is — the
  partial unique index ``jobs_one_live_per_document`` makes a second live
  job unrepresentable, and the race between check and insert is settled by
  that index, not by the check;
- a document whose latest job succeeded is NOT re-processed (re-uploading
  the same PDF is a no-op end to end); the succeeded job is returned;
- a document whose latest job failed gets a fresh job — re-uploading after
  an outage is the retry path.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg import errors
from psycopg.types.json import Jsonb

from tax_tables.adapters.anthropic_adjudicator import AdjudicatorConfig, AnthropicAdjudicator
from tax_tables.adapters.anthropic_mapper import (
    AnthropicSchemaMapper,
    MapperConfig,
    MapperConfigError,
    MapperError,
)
from tax_tables.adapters.anthropic_verifier import (
    AnthropicRecordVerifier,
    VerifierConfig,
    VerifierError,
)
from tax_tables.adapters.pdfplumber_extractor import PdfplumberExtractor
from tax_tables.adapters.postgres import PostgresRecordRepository
from tax_tables.adapters.tesseract_extractor import TesseractExtractor
from tax_tables.extraction.router import ExtractionRouter
from tax_tables.pipeline import run_document


@dataclass(frozen=True)
class EnqueueOutcome:
    document_id: UUID
    job_id: UUID
    #: False when an existing job (live or already succeeded) was returned
    #: instead of a new one being created.
    created: bool


def enqueue_ingest(
    repository: PostgresRecordRepository,
    *,
    pdf_bytes: bytes,
    filename: str,
    sha256: str,
    page_count: int,
) -> EnqueueOutcome:
    handle = repository.register_document(
        sha256=sha256,
        filename=filename,
        byte_size=len(pdf_bytes),
        page_count=page_count,
    )
    repository.store_blob(handle.id, pdf_bytes)
    conn = repository.connection
    with conn.transaction():
        latest = conn.execute(
            """
            SELECT id, status FROM jobs
            WHERE document_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (handle.id,),
        ).fetchone()
        if latest is not None and latest[1] in ("queued", "running", "succeeded"):
            return EnqueueOutcome(document_id=handle.id, job_id=latest[0], created=False)
        try:
            with conn.transaction():  # savepoint: the unique index settles races
                row = conn.execute(
                    "INSERT INTO jobs (document_id) VALUES (%s) RETURNING id",
                    (handle.id,),
                ).fetchone()
                assert row is not None
                return EnqueueOutcome(document_id=handle.id, job_id=row[0], created=True)
        except errors.UniqueViolation:
            live = conn.execute(
                """
                SELECT id FROM jobs
                WHERE document_id = %s AND status IN ('queued', 'running')
                """,
                (handle.id,),
            ).fetchone()
            assert live is not None  # the index just proved a live job exists
            return EnqueueOutcome(document_id=handle.id, job_id=live[0], created=False)


def _safe_error(kind: str, exc: BaseException) -> dict[str, Any]:
    """Error payload for the jobs table: class and message, never a value
    from the environment. Config errors already speak in env-var names."""
    return {"type": kind, "error_class": type(exc).__name__, "message": str(exc)}


def process_job(
    dsn: str,
    job_id: UUID,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Run one job's pipeline; returns the final status.

    ``env`` overrides ``os.environ`` for adapter construction — tests pass a
    controlled mapping so the missing-credentials path is deterministic
    regardless of the developer's shell.
    """
    source = os.environ if env is None else env
    with psycopg.connect(dsn, connect_timeout=30) as conn:
        with conn.transaction():
            claimed = conn.execute(
                """
                UPDATE jobs
                SET status = 'running', attempt = attempt + 1, started_at = now()
                WHERE id = %s AND status IN ('queued', 'running')
                RETURNING document_id
                """,
                (job_id,),
            ).fetchone()
        if claimed is None:
            return "not_claimable"
        document_id: UUID = claimed[0]

        def finish(status: str, *, error: dict[str, Any] | None = None, **counts: int) -> str:
            with conn.transaction():
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = %s, error = %s, finished_at = now(),
                        records_extracted = %s, records_persisted = %s, review_count = %s
                    WHERE id = %s
                    """,
                    (
                        status,
                        None if error is None else Jsonb(error),
                        counts.get("records_extracted"),
                        counts.get("records_persisted"),
                        counts.get("review_count"),
                        job_id,
                    ),
                )
            return status

        try:
            mapper_config = MapperConfig.from_env(source)
        except MapperConfigError as exc:
            return finish("failed", error=_safe_error("missing_credentials", exc))

        with PostgresRecordRepository(dsn) as repository:
            try:
                pdf_bytes = repository.load_blob(document_id)
                filename_row = conn.execute(
                    "SELECT filename FROM documents WHERE id = %s", (document_id,)
                ).fetchone()
                assert filename_row is not None
                result = run_document(
                    pdf_bytes,
                    filename=filename_row[0],
                    router=ExtractionRouter(
                        digital=PdfplumberExtractor(), ocr=TesseractExtractor()
                    ),
                    mapper=AnthropicSchemaMapper(mapper_config),
                    verifier=AnthropicRecordVerifier(VerifierConfig.from_env(source)),
                    repository=repository,
                    adjudicator=AnthropicAdjudicator(AdjudicatorConfig.from_env(source)),
                )
            except (MapperError, VerifierError) as exc:
                return finish("failed", error=_safe_error("semantic_layer_failed", exc))
            except Exception as exc:  # the job row must always report ITS fate
                return finish("failed", error=_safe_error("internal", exc))

        ingest = result.ingest
        assert ingest is not None  # a repository was supplied
        return finish(
            "succeeded",
            records_extracted=len(result.mapping.records),
            records_persisted=ingest.persisted,
            review_count=result.review_entries,
        )


def sweep_pending(
    dsn: str,
    *,
    env: Mapping[str, str] | None = None,
    limit: int = 10,
) -> list[UUID]:
    """Claim up to ``limit`` queued jobs and process each. The cron-sweep
    JobRunner on Vercel and the docker-compose worker loop both call this;
    ``FOR UPDATE SKIP LOCKED`` keeps concurrent sweepers off each other's
    jobs."""
    with psycopg.connect(dsn, connect_timeout=30) as conn, conn.transaction():
        rows = conn.execute(
            """
            SELECT id FROM jobs
            WHERE status = 'queued'
            ORDER BY created_at, id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (limit,),
        ).fetchall()
        job_ids = [row[0] for row in rows]
    processed: list[UUID] = []
    for job_id in job_ids:
        process_job(dsn, job_id, env=env)
        processed.append(job_id)
    return processed
