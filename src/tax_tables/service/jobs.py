"""Job lifecycle: enqueue on upload, process by sweep or worker.

``POST /documents`` calls :func:`enqueue_ingest` and returns 202 — the
request never blocks on extraction. Processing happens elsewhere:
:func:`sweep_pending` claims jobs with ``FOR UPDATE SKIP LOCKED`` (safe
under concurrent sweepers) and runs each through :func:`process_job`, which
is where the pipeline's adapters are built from the environment.

A job is claimable when it is ``queued`` **or** when it is ``running`` and
its lease has expired — a visibility timeout. The second half is not
theoretical: on request-scoped compute the platform kills a worker at
``maxDuration`` and nothing rewrites the row, so a queued-only sweep is
blind to precisely the jobs it exists to rescue. See
:data:`DEFAULT_LEASE_SECONDS` for the invariant that keeps a live worker's
job from being stolen.

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
  an outage is the retry path. This is why the lease matters end to end: a
  stranded ``running`` job reads as live, so until the sweep can move it to
  a terminal state, the sha256 natural key hands back the stranded job and
  the document cannot be re-ingested at all.
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
from tax_tables.extraction.router import ExtractionRouter
from tax_tables.pipeline import run_document
from tax_tables.ports.extractor import TableExtractor


def ocr_extractor(source: Mapping[str, str]) -> TableExtractor:
    """The target's pixel-licensed adapter, chosen by config.

    One knob, three targets (``EXTRACTION_OCR_ENGINE``): ``tesseract``
    locally, ``vision`` on Vercel — where no system binary can exist, so the
    tesseract executable simply cannot be installed (ADR 010) — and
    ``textract`` on AWS. Imports are local to each branch so that choosing
    one target never drags another's dependency into the bundle: boto3 and
    pytesseract must both stay out of the Vercel function.

    An unknown value raises rather than defaulting. Silently falling back to
    a local binary that is not present would surface as an empty document at
    high confidence — the exact silent loss anti-goal #8 forbids.
    """
    engine = (source.get("EXTRACTION_OCR_ENGINE") or "tesseract").strip().lower()
    if engine == "tesseract":
        from tax_tables.adapters.tesseract_extractor import TesseractExtractor

        return TesseractExtractor()
    if engine == "vision":
        from tax_tables.adapters.vision_extractor import AnthropicVisionExtractor, VisionOcrConfig

        return AnthropicVisionExtractor(VisionOcrConfig.from_env(source))
    if engine == "textract":
        from tax_tables.adapters.textract_extractor import TextractExtractor

        return TextractExtractor()
    raise ValueError(
        f"unknown EXTRACTION_OCR_ENGINE {engine!r}: expected tesseract, vision, or textract"
    )


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
    lease_seconds = _int_setting(source, "JOB_LEASE_SECONDS", DEFAULT_LEASE_SECONDS)
    with psycopg.connect(dsn, connect_timeout=30) as conn:
        with conn.transaction():
            # The claim carries the SAME lease predicate the sweep selects on,
            # and it has to: `sweep_pending`'s `FOR UPDATE SKIP LOCKED`
            # transaction COMMITS when its SELECT returns, so the row locks are
            # gone before any work begins. Without this predicate a second
            # sweeper arriving mid-pipeline re-claimed a live worker's job,
            # bumped `attempt`, and ran the document again concurrently — at
            # the shipped settings (a 60 s cron over documents measured at
            # 346 s) that overlap is the steady state, not an edge case.
            #
            # The lock guards the SELECT; this predicate guards the CLAIM.
            # Both are the design (ADR 009, annotated 2026-08-27).
            claimed = conn.execute(
                """
                UPDATE jobs
                SET status = 'running', attempt = attempt + 1, started_at = now()
                WHERE id = %s
                  AND (
                        status = 'queued'
                        OR (
                             status = 'running'
                             AND started_at IS NOT NULL
                             AND started_at < now() - make_interval(secs => %s)
                           )
                      )
                RETURNING document_id
                """,
                (job_id, lease_seconds),
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
                        digital=PdfplumberExtractor(), ocr=ocr_extractor(source)
                    ),
                    mapper=AnthropicSchemaMapper(mapper_config),
                    verifier=AnthropicRecordVerifier(VerifierConfig.from_env(source)),
                    repository=repository,
                    adjudicator=AnthropicAdjudicator(AdjudicatorConfig.from_env(source)),
                    adjudication_budget_seconds=_float_setting(
                        source, "ADJUDICATION_BUDGET_SECONDS", DEFAULT_ADJUDICATION_BUDGET_SECONDS
                    ),
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


#: How long a claimed job is leased to the worker that claimed it.
#:
#: **INVARIANT: this must be >= the platform's function ``maxDuration``.** A
#: worker cannot outlive ``maxDuration``, so any ``running`` job older than
#: that has certainly lost its worker. A *shorter* lease is the dangerous
#: direction: the sweep would reclaim a job whose worker is still alive, two
#: workers would map the same document, and the run would be paid for twice.
#: ``vercel.json`` sets ``maxDuration`` to 1800 s; this is that plus a minute.
DEFAULT_LEASE_SECONDS = 1860

#: How many times a job may be attempted before it is abandoned as failed.
#: Without a ceiling, a document that reliably kills its worker is reclaimed
#: forever and every reclaim spends model credit.
DEFAULT_MAX_ATTEMPTS = 3


#: Wall-clock ceiling on the post-persistence adjudication pass.
#:
#: Adjudication is optional by design — an item it cannot settle waits for a
#: human — but its duration is unbounded: one call can spend the adapter's
#: request timeout and the SDK retries it. On request-scoped compute that lets
#: a slow queue eat the whole function budget so the job never finishes,
#: which is strictly worse than leaving a queue item for its human. Measured
#: on production 2026-08-27: document 01 persisted at ~360 s and then spent
#: the remainder of a 1800 s invocation adjudicating, twice, terminating
#: neither time.
DEFAULT_ADJUDICATION_BUDGET_SECONDS = 420.0


def _float_setting(source: Mapping[str, str], name: str, default: float) -> float:
    raw = source.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _int_setting(source: Mapping[str, str], name: str, default: int) -> int:
    raw = source.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _abandon(dsn: str, job_id: UUID, attempts: int) -> None:
    """Terminal state for a job that has burned its attempts. It is marked,
    never deleted: the row is the only evidence the work was lost."""
    with psycopg.connect(dsn, connect_timeout=30) as conn, conn.transaction():
        conn.execute(
            """
            UPDATE jobs
            SET status = 'failed', finished_at = now(), error = %s
            WHERE id = %s
            """,
            (
                Jsonb(
                    {
                        "type": "lease_expired_max_attempts",
                        "error_class": "LeaseExpired",
                        "message": (
                            f"job lost its worker and reached {attempts} attempts; "
                            "abandoned rather than reclaimed again"
                        ),
                    }
                ),
                job_id,
            ),
        )


def sweep_pending(
    dsn: str,
    *,
    env: Mapping[str, str] | None = None,
    limit: int = 10,
) -> list[UUID]:
    """Claim up to ``limit`` claimable jobs and process each. The cron-sweep
    JobRunner on Vercel and the local sweep both call this.

    ``FOR UPDATE SKIP LOCKED`` keeps concurrent sweepers off each other's
    rows **while this SELECT runs** — and only then. The transaction commits
    when the SELECT returns, so by the time the loop below starts working the
    locks are gone. That is why the claim in :func:`process_job` carries the
    lease predicate too: the lock guards the SELECT, the predicate guards the
    CLAIM, and a sweeper arriving mid-pipeline is turned away by the second,
    never by the first.

    **Claimable is not the same as queued.** A job whose worker the platform
    killed stays ``running`` forever — nothing rewrites the row, because the
    process that would have written it is gone. Selecting only ``queued``
    made the cron backstop blind to exactly the failure it exists to cover;
    five production jobs were stranded that way on 2026-08-27. So a
    ``running`` job whose lease has expired is claimable too, which is the
    visibility-timeout semantics Step Functions gives the AWS target for
    free.
    """
    source = os.environ if env is None else env
    lease_seconds = _int_setting(source, "JOB_LEASE_SECONDS", DEFAULT_LEASE_SECONDS)
    max_attempts = _int_setting(source, "JOB_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)
    with psycopg.connect(dsn, connect_timeout=30) as conn, conn.transaction():
        rows = conn.execute(
            """
            SELECT id, attempt FROM jobs
            WHERE status = 'queued'
               OR (
                    status = 'running'
                    AND started_at IS NOT NULL
                    AND started_at < now() - make_interval(secs => %s)
                  )
            ORDER BY created_at, id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (lease_seconds, limit),
        ).fetchall()
        claimable = [(row[0], row[1]) for row in rows]
    processed: list[UUID] = []
    for job_id, attempt in claimable:
        if attempt >= max_attempts:
            _abandon(dsn, job_id, attempt)
        else:
            process_job(dsn, job_id, env=env)
        processed.append(job_id)
    return processed
