"""Lambda entry points — the deploy-time artifact contract the CDK stack
addresses (``infra/tax_tables_stack.py`` handler strings resolve here; a
test pins template string <-> callable).

The AWS target recomposes the SAME pipeline the other targets run, split at
the Step Functions state boundaries:

    api (Mangum over the Phase 3 FastAPI app; StepFunctionsJobRunner)
      -> extract_document  (router: pdfplumber at $0, Textract for scans)
      -> map_and_verify    (Bedrock mapper + independent verifier)
      -> persist_records   (triage + repository via RDS Proxy)
      -> adjudicate_queue  (Bedrock adjudicator over open items)

Payloads between states are the models' own JSON dumps
(``ExtractedDocument`` / ``CanonicalRecord`` / ``VerificationResult``), so
no step ever re-reads pixels and every value stays traceable. Collaborators
(boto3 clients, adapters, the repository) are keyword-injectable for the
unit tests; the zero-argument path builds them from the Lambda environment.
boto3 and mangum import lazily: they ship in the Lambda runtime / the
``aws`` extra and must not be import-time requirements elsewhere.

Database identity: the proxy DSN authenticates as the least-privilege
``app_ingest`` role with an IAM auth token — ``generate_db_auth_token`` is
LOCAL SigV4 signing, no network call (the stack's endpoint inventory relies
on this). Tokens are minted per invocation; they expire in 15 minutes.

Known deploy-pipeline gaps (README, honest limitations): the dependency
layer build, provisioning of API_KEY/CRON_SECRET, creation of the
``app_ingest`` role, and payload offloading to S3 for documents whose
extracted JSON approaches the Step Functions 256 KB payload cap.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import quote
from uuid import UUID

from aws_lambda_powertools import Logger
from psycopg.types.json import Jsonb

from tax_tables.adapters.bedrock import bedrock_adjudicator, bedrock_mapper, bedrock_verifier
from tax_tables.adapters.pdfplumber_extractor import PdfplumberExtractor
from tax_tables.adapters.postgres import PostgresRecordRepository
from tax_tables.adapters.textract_extractor import TextractExtractor
from tax_tables.extraction.model import ExtractedDocument
from tax_tables.extraction.router import ExtractionRouter
from tax_tables.pipeline import adjudicate_open_items, dispute_findings, issue_entry
from tax_tables.ports.adjudicator import DEFAULT_AUTO_RESOLVE_THRESHOLD, Adjudicator
from tax_tables.ports.mapper import MappingIssue, MappingResult, SchemaMapper
from tax_tables.ports.verifier import RecordVerifier, VerificationResult
from tax_tables.validation.validators import Finding, review_queue_entry, triage

if TYPE_CHECKING:
    import psycopg

from tax_tables.domain.records import CanonicalRecord

#: Powertools Logger — the one part of the Lambda toolkit this project
#: actually adopts (ADR 013 addendum): JSON lines keyed by service, with
#: per-document correlation keys bound by ``_bind`` below. Idempotency lives
#: at the data layer and batch semantics at the Distributed Map, so neither
#: of the toolkit's other two utilities has anything here to attach to.
#:
#: Deliberately NOT ``@logger.inject_lambda_context``: that decorator makes
#: ``context`` a required positional argument (verified), which would break
#: the keyword-injectable collaborators every handler test depends on.
#: ``_bind`` buys the same correlation without touching the signatures.
logger = Logger(service="tax-tables")


def _bind(event: Mapping[str, Any], context: Any = None) -> None:
    """Attach this document's identity to every subsequent log line.

    In a Distributed Map fan-out the hard question is never "did something
    fail" — the alarm answers that — but "which document, in which step".
    These keys are that answer.
    """
    keys: dict[str, Any] = {}
    for name in ("job_id", "document_id"):
        if event.get(name) is not None:
            keys[name] = str(event[name])
    request_id = getattr(context, "aws_request_id", None)
    if request_id is not None:
        keys["function_request_id"] = request_id
    if keys:
        logger.append_keys(**keys)


# ---------------------------------------------------------------------------
# Environment plumbing
# ---------------------------------------------------------------------------


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    if env is not None:
        return env
    import os

    return os.environ


def auth_token(env: Mapping[str, str] | None = None) -> str:
    """A fresh RDS IAM auth token for the proxy — local SigV4 signing via
    boto3, no network traffic (no endpoint needed)."""
    import boto3  # Lambda runtime / `aws` extra

    source = _env(env)
    client = boto3.client("rds")
    token: str = client.generate_db_auth_token(
        DBHostname=source["DB_PROXY_ENDPOINT"],
        Port=int(source["DB_PORT"]),
        DBUsername=source["DB_USER"],
    )
    return token


def proxy_dsn(env: Mapping[str, str] | None = None, *, token: str | None = None) -> str:
    """The per-invocation DSN: app_ingest + IAM token against the proxy,
    TLS required (the proxy enforces it; the DSN states it anyway)."""
    source = _env(env)
    password = token if token is not None else auth_token(source)
    return (
        f"postgresql://{quote(source['DB_USER'])}:{quote(password, safe='')}"
        f"@{source['DB_PROXY_ENDPOINT']}:{source['DB_PORT']}/{source['DB_NAME']}"
        "?sslmode=require"
    )


def _repository(env: Mapping[str, str] | None) -> PostgresRecordRepository:
    return PostgresRecordRepository(proxy_dsn(env))


# ---------------------------------------------------------------------------
# Job bookkeeping (the split-step twin of service.jobs.process_job's
# claim/finish — that function owns the single-process targets; these two
# statements own the Step Functions target)
# ---------------------------------------------------------------------------


def _mark_running(conn: psycopg.Connection[Any], job_id: str) -> None:
    with conn.transaction():
        conn.execute(
            "UPDATE jobs SET status = 'running', attempt = attempt + 1,"
            " started_at = now() WHERE id = %s AND status IN ('queued', 'running')",
            (job_id,),
        )


#: Upper bound on the Step Functions ``Cause`` text stored in ``jobs.error``.
#: A Cause carries a Lambda stack trace; the column is a failure report the
#: API serves, not a log sink.
MAX_STORED_CAUSE_CHARS = 2000


def _fail_job(conn: psycopg.Connection[Any], job_id: str, error: Mapping[str, Any]) -> None:
    """Record a failed pipeline step against the job row.

    The guard is the point: ``AdjudicateStep`` runs after ``PersistStep``,
    so an adjudication failure finds a job that already succeeded with its
    records in the fact table. Reopening it would report persisted data as
    lost; the open review-queue items are that failure's honest signal.
    Only a job still queued or running is closed here.

    Shape matches ``service.jobs._safe_error`` — the single-process
    targets' twin — so ``GET /jobs/{id}`` reads the same on every target.
    """
    payload = {
        "type": "pipeline_step_failed",
        "error_class": str(error.get("Error", "Unknown")),
        "message": str(error.get("Cause", ""))[:MAX_STORED_CAUSE_CHARS],
    }
    with conn.transaction():
        conn.execute(
            "UPDATE jobs SET status = 'failed', error = %s, finished_at = now()"
            " WHERE id = %s AND status IN ('queued', 'running')",
            (Jsonb(payload), job_id),
        )


def _finish_job(conn: psycopg.Connection[Any], job_id: str, **counts: int) -> None:
    with conn.transaction():
        conn.execute(
            "UPDATE jobs SET status = 'succeeded', finished_at = now(),"
            " records_extracted = %s, records_persisted = %s, review_count = %s"
            " WHERE id = %s",
            (
                counts.get("records_extracted"),
                counts.get("records_persisted"),
                counts.get("review_count"),
                job_id,
            ),
        )


# ---------------------------------------------------------------------------
# JobRunner port — Step Functions adapter
# ---------------------------------------------------------------------------


class StepFunctionsJobRunner:
    """JobRunner over Step Functions: stage the blob to S3 (the AWS blob
    home, and what Textract's extract step reads) and start one pipeline
    execution. ``notify`` never raises past the caller — the job row is the
    source of truth, and a lost notification leaves the job ``queued`` for
    recovery, never lost (port contract)."""

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        s3: Any | None = None,
        sfn: Any | None = None,
        repository: PostgresRecordRepository | None = None,
    ) -> None:
        self._envmap = env
        self._s3 = s3
        self._sfn = sfn
        self._repository = repository

    def _clients(self) -> tuple[Any, Any]:
        if self._s3 is None or self._sfn is None:
            import boto3

            self._s3 = self._s3 or boto3.client("s3")
            self._sfn = self._sfn or boto3.client("stepfunctions")
        return self._s3, self._sfn

    def notify(self, job_id: Any) -> None:
        try:
            source = _env(self._envmap)
            repository = self._repository or _repository(self._envmap)
            row = repository.connection.execute(
                "SELECT j.document_id, d.filename FROM jobs j"
                " JOIN documents d ON d.id = j.document_id WHERE j.id = %s",
                (job_id,),
            ).fetchone()
            if row is None:
                logger.error("job %s vanished before notification", job_id)
                return
            document_id, filename = row
            blob = repository.load_blob(document_id)
            bucket = source["DOCUMENTS_BUCKET"]
            key = f"documents/{document_id}.pdf"
            s3, sfn = self._clients()
            s3.put_object(Bucket=bucket, Key=key, Body=blob, ContentType="application/pdf")
            sfn.start_execution(
                stateMachineArn=source["PIPELINE_STATE_MACHINE_ARN"],
                input=json.dumps(
                    {
                        "documents": [
                            {
                                "job_id": str(job_id),
                                "document_id": str(document_id),
                                "bucket": bucket,
                                "key": key,
                                "filename": filename,
                            }
                        ]
                    }
                ),
            )
        except Exception:
            # The queued row remains; a re-upload or operator retry recovers
            # it. Raising here would fail the 202 the client already earned.
            logger.exception("job %s: pipeline notification failed", job_id)


# ---------------------------------------------------------------------------
# The five Lambda handlers
# ---------------------------------------------------------------------------


def api(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """API Gateway proxy -> the Phase 3 FastAPI app, via Mangum.

    Rebuilt per invocation: the DSN embeds an IAM token with a 15-minute
    lifetime, so a cold-start-cached app would go stale mid-life."""
    import os

    from mangum import Mangum

    from tax_tables.api.app import create_app
    from tax_tables.api.settings import ApiSettings

    env = dict(os.environ)
    env.setdefault("DATABASE_URL", proxy_dsn(env))
    settings = ApiSettings.from_env(env)
    runner = StepFunctionsJobRunner(env=env)
    asgi = Mangum(create_app(settings, runner=runner), lifespan="off")
    return asgi(event, context)


def extract_document(
    event: dict[str, Any],
    context: Any = None,
    *,
    s3: Any | None = None,
    textract: Any | None = None,
    repository: PostgresRecordRepository | None = None,
) -> dict[str, Any]:
    """S3 bytes -> extraction router. The router's economics hold on AWS
    exactly as everywhere else: a text-layer document never reaches the
    paid Textract path (a test pins zero Textract calls for digital
    input)."""
    _bind(event, context)
    if s3 is None:
        import boto3

        s3 = boto3.client("s3")
    repository = repository or _repository(None)
    _mark_running(repository.connection, event["job_id"])
    body = s3.get_object(Bucket=event["bucket"], Key=event["key"])["Body"].read()
    router = ExtractionRouter(digital=PdfplumberExtractor(), ocr=TextractExtractor(client=textract))
    extracted = router.extract(body, filename=event["filename"])
    # The router's economics, per document and in the log: a text-layer
    # document must show engine=pdfplumber and api_calls=0.
    logger.info(
        "extracted",
        extra={
            "engine": extracted.cost.engine,
            "api_calls": extracted.cost.api_calls,
            "usd": str(extracted.cost.usd),
            "pages": len(extracted.pages),
        },
    )
    return {**event, "extracted": extracted.model_dump(mode="json")}


def map_and_verify(
    event: dict[str, Any],
    context: Any = None,
    *,
    mapper: SchemaMapper | None = None,
    verifier: RecordVerifier | None = None,
) -> dict[str, Any]:
    """Bedrock mapper + independent verifier over the extracted grid — the
    bounded ADR 012 pair, unchanged; only the client transport differs."""
    _bind(event, context)
    extracted = ExtractedDocument.model_validate(event["extracted"])
    mapping = (mapper or bedrock_mapper()).map_document(extracted)
    verification = (verifier or bedrock_verifier()).verify(extracted, mapping)
    payload = {
        **event,
        "mapping": {
            "records": [r.model_dump(mode="json") for r in mapping.records],
            "issues": [i.model_dump(mode="json") for i in mapping.issues],
            "cost": None if mapping.cost is None else mapping.cost.model_dump(mode="json"),
        },
        "verification": verification.model_dump(mode="json"),
    }
    logger.info(
        "mapped and verified",
        extra={
            "records": len(mapping.records),
            "mapping_issues": len(mapping.issues),
            "disputes": sum(1 for v in verification.verdicts if v.verdict == "disputed"),
        },
    )
    return payload


def persist_records(
    event: dict[str, Any],
    context: Any = None,
    *,
    repository: PostgresRecordRepository | None = None,
) -> dict[str, Any]:
    """Triage + persist + queue — the persist half of ``run_document``,
    recomposed from the same public pieces, same accounting invariant:
    every record persists or lands in the queue with its reason."""
    _bind(event, context)
    repository = repository or _repository(None)
    # CanonicalRecord is strict: python-mode validation refuses the JSON
    # dump's strings ("0.95", "clean"), so the round-trip goes through JSON
    # validation — the same rules the payload was serialized under.
    records = [
        CanonicalRecord.model_validate_json(json.dumps(r)) for r in event["mapping"]["records"]
    ]
    issues = [MappingIssue.model_validate(i) for i in event["mapping"]["issues"]]
    mapping = MappingResult(records=records, issues=issues)
    verification = VerificationResult.model_validate(event["verification"])
    disputes: list[Finding] = dispute_findings(verification)
    triaged = triage(mapping.records, extra_findings=disputes)

    document_id = UUID(str(event["document_id"]))
    outcome = repository.ingest(document_id, triaged.persistable)
    entries = [
        review_queue_entry(mapping.records[finding.record_index], finding)
        for finding in triaged.findings
    ]
    entries.extend(issue_entry(issue) for issue in mapping.issues)
    queued = repository.queue_review(document_id, entries) if entries else 0
    _finish_job(
        repository.connection,
        event["job_id"],
        records_extracted=len(mapping.records),
        records_persisted=outcome.persisted,
        review_count=queued,
    )
    # The accounting invariant, in the log: every mapped record is either
    # persisted or queued, and this line is where an operator sees it hold.
    logger.info(
        "persisted",
        extra={
            "mapped": len(mapping.records),
            "persisted": outcome.persisted,
            "queued_for_review": queued,
            "overlap_rejections": outcome.overlap_rejections,
            "cross_document_conflicts": outcome.cross_document_conflicts,
        },
    )
    return {
        **event,
        "persisted": outcome.persisted,
        "cross_document_conflicts": outcome.cross_document_conflicts,
        "overlap_rejections": outcome.overlap_rejections,
        "review_entries": queued,
    }


def adjudicate_queue(
    event: dict[str, Any],
    context: Any = None,
    *,
    repository: PostgresRecordRepository | None = None,
    adjudicator: Adjudicator | None = None,
) -> dict[str, Any]:
    """The single adjudication pass over the document's open queue items —
    ``adjudicate_open_items`` with Bedrock behind it; per-item containment
    and the FLAG-only auto-resolve rule are the shared pipeline's."""
    _bind(event, context)
    repository = repository or _repository(None)
    extracted = ExtractedDocument.model_validate(event["extracted"])
    outcomes = adjudicate_open_items(
        repository,
        adjudicator or bedrock_adjudicator(),
        UUID(str(event["document_id"])),
        extracted,
        DEFAULT_AUTO_RESOLVE_THRESHOLD,
    )
    dispositions: dict[str, int] = {}
    for outcome in outcomes:
        dispositions[outcome.disposition] = dispositions.get(outcome.disposition, 0) + 1
    logger.info("adjudicated", extra={"items": len(outcomes), "dispositions": dispositions})
    return {
        "job_id": event["job_id"],
        "document_id": str(event["document_id"]),
        "adjudications": [
            {"item_id": str(o.item_id), "disposition": o.disposition, "error": o.error}
            for o in outcomes
        ],
    }


def mark_job_failed(
    event: dict[str, Any],
    context: Any = None,
    *,
    repository: PostgresRecordRepository | None = None,
) -> dict[str, Any]:
    """Every pipeline step's ``Catch`` target: close the job with its reason.

    ``tolerated_failure_percentage=100`` on the Distributed Map is a
    deliberate transport choice — AWS documents that at 100 "the workflow
    won't fail even if all child workflow executions fail", which is
    exactly the per-document isolation the local pipeline enforces. It is
    only safe because the failure is recorded elsewhere: this handler
    writes it to the job row, and the state that follows still fails the
    child execution so the batch-level metric and its alarm can see it.
    """
    _bind(event, context)
    repository = repository or _repository(None)
    error = event.get("error") or {}
    # The one line an operator greps for after the DocumentFailures alarm.
    logger.warning("document failed", extra={"error_type": str(error.get("Error", "Unknown"))})
    _fail_job(repository.connection, str(event["job_id"]), error)
    return {
        "job_id": str(event["job_id"]),
        "document_id": str(event.get("document_id", "")),
        "status": "failed",
    }
