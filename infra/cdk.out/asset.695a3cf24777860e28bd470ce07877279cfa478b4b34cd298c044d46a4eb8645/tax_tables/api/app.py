"""The FastAPI application (Phase 3).

Surface (CLAUDE.md): ``POST /documents`` -> 202 + job id, never blocking on
extraction; ``GET /jobs/{id}``; ``GET /records`` with filters and cursor
pagination; ``GET /documents`` (+ ``/{id}``) provenance; ``GET
/records/resolve``. Hardening: the POST requires ``X-API-Key``; GETs stay
public and read-only; the sweep endpoint (the cron/queue-subscriber path)
takes a ``CRON_SECRET`` bearer; uploads are rejected before any byte
reaches the pipeline when oversized, not ``%PDF``-prefixed, or over the
page cap. Per-IP rate limiting is a Vercel Firewall rule (Phase 3.5), not
application code.

Connections are opened per request and closed with it — the request-scoped
model the Vercel target enforces anyway, and the local target tolerates.

(No ``from __future__ import annotations`` here: FastAPI resolves dependency
annotations at runtime, and the stringized form cannot see function-local
aliases like ``Conn``.)
"""

import base64
import binascii
import hashlib
import io
import secrets
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

import pdfplumber
import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response

from tax_tables.adapters.postgres import PostgresRecordRepository
from tax_tables.api import queries
from tax_tables.api.schemas import (
    DocumentOut,
    IngestAccepted,
    JobOut,
    RecordOut,
    RecordsPage,
    ResolveOut,
    ReviewOut,
    ReviewsPage,
    SweepOut,
)
from tax_tables.api.settings import MAX_PAGE_SIZE, ApiSettings
from tax_tables.domain.records import FilingStatus, RecordType, ReviewQueueStatus
from tax_tables.ports.jobs import JobRunner, NullJobRunner
from tax_tables.service.jobs import enqueue_ingest, sweep_pending

_PDF_MAGIC = b"%PDF"


def _encode_cursor(created_at: datetime, record_id: UUID) -> str:
    raw = f"{created_at.isoformat()}|{record_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode()
        created_at_text, _, id_text = raw.partition("|")
        return datetime.fromisoformat(created_at_text), UUID(id_text)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(status_code=400, detail="malformed cursor") from exc


def create_app(settings: ApiSettings, *, runner: JobRunner | None = None) -> FastAPI:
    job_runner = runner if runner is not None else NullJobRunner()
    app = FastAPI(
        title="Tax Table Ingestion Service",
        version="0.1.0",
        description=(
            "Accepts PDF documents containing tax tables, extracts and "
            "normalizes the tabular data, and exposes the canonical records. "
            "Uploads are processed asynchronously: POST /documents returns "
            "202 with a job id; poll GET /jobs/{job_id}."
        ),
    )

    def connection() -> Iterator[psycopg.Connection[Any]]:
        conn = psycopg.connect(settings.database_url, connect_timeout=30)
        try:
            yield conn
        finally:
            conn.close()

    Conn = Annotated[psycopg.Connection[Any], Depends(connection)]

    def require_api_key(
        x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    ) -> None:
        if x_api_key is None or not secrets.compare_digest(x_api_key, settings.api_key):
            raise HTTPException(status_code=401, detail="missing or invalid X-API-Key")

    def require_cron_secret(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        expected = f"Bearer {settings.cron_secret}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")

    @app.post(
        "/documents",
        status_code=202,
        response_model=IngestAccepted,
        dependencies=[Depends(require_api_key)],
        summary="Upload a PDF for ingestion (asynchronous)",
    )
    async def upload_document(request: Request, conn: Conn) -> IngestAccepted:
        # Guards run cheapest-first, and every one of them before a byte
        # reaches the pipeline (CLAUDE.md hardening).
        declared = request.headers.get("content-length")
        if (
            declared is not None
            and declared.isdigit()
            and int(declared) > settings.max_upload_bytes
        ):
            raise HTTPException(status_code=413, detail="upload exceeds the size limit")
        body = await request.body()
        if len(body) > settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="upload exceeds the size limit")
        if not body.startswith(_PDF_MAGIC):
            raise HTTPException(status_code=415, detail="not a PDF (missing %PDF header)")
        try:
            with pdfplumber.open(io.BytesIO(body)) as pdf:
                page_count = len(pdf.pages)
        except Exception as exc:
            raise HTTPException(status_code=415, detail="not a parsable PDF") from exc
        if page_count > settings.max_pages:
            raise HTTPException(
                status_code=413,
                detail=f"{page_count} pages exceeds the cap of {settings.max_pages}",
            )

        filename = request.headers.get("x-filename", "upload.pdf")
        repository = PostgresRecordRepository.from_connection(conn)
        outcome = enqueue_ingest(
            repository,
            pdf_bytes=body,
            filename=filename,
            sha256=hashlib.sha256(body).hexdigest(),
            page_count=page_count,
        )
        if outcome.created:
            job_runner.notify(outcome.job_id)
        return IngestAccepted(
            document_id=outcome.document_id,
            job_id=outcome.job_id,
            duplicate=not outcome.created,
        )

    @app.get("/jobs/{job_id}", response_model=JobOut, summary="Job status and counts")
    def read_job(job_id: UUID, conn: Conn) -> JobOut:
        row = queries.get_job(conn, job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such job")
        return JobOut(**row)

    @app.get("/documents", response_model=list[DocumentOut], summary="Uploaded documents")
    def read_documents(conn: Conn) -> list[DocumentOut]:
        return [DocumentOut(**row) for row in queries.list_documents(conn)]

    @app.get("/documents/{document_id}", response_model=DocumentOut, summary="One document")
    def read_document(document_id: UUID, conn: Conn) -> DocumentOut:
        row = queries.get_document(conn, document_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such document")
        return DocumentOut(**row)

    @app.get("/records", response_model=RecordsPage, summary="Canonical records")
    def read_records(
        conn: Conn,
        response: Response,
        tax_year: Annotated[int | None, Query(ge=1900, le=2999)] = None,
        jurisdiction: Annotated[str | None, Query(min_length=2)] = None,
        record_type: RecordType | None = None,
        filing_status: FilingStatus | None = None,
        effective_on: date | None = None,
        include_superseded: bool = False,
        min_confidence: Annotated[Decimal | None, Query(ge=0, le=1)] = None,
        cursor: str | None = None,
        limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)] = None,
    ) -> RecordsPage:
        page_size = min(limit or settings.default_page_size, settings.max_page_size)
        rows = queries.list_records(
            conn,
            tax_year=tax_year,
            jurisdiction=jurisdiction,
            record_type=None if record_type is None else record_type.value,
            filing_status=None if filing_status is None else filing_status.value,
            effective_on=effective_on,
            include_superseded=include_superseded,
            min_confidence=min_confidence,
            after=None if cursor is None else _decode_cursor(cursor),
            limit=page_size + 1,  # one extra row decides whether a next page exists
        )
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        next_cursor = (
            _encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if has_more and rows else None
        )
        response.headers["Cache-Control"] = "no-store"
        return RecordsPage(items=[RecordOut(**row) for row in rows], next_cursor=next_cursor)

    @app.get(
        "/records/resolve",
        response_model=ResolveOut,
        summary="The bracket containing an amount (a data lookup, not tax advice)",
    )
    def resolve(
        conn: Conn,
        amount: Annotated[int, Query(ge=0)],
        tax_year: Annotated[int, Query(ge=1900, le=2999)],
        filing_status: FilingStatus | None = None,
        taxpayer_class: str | None = None,
        jurisdiction: str = "US",
        record_type: RecordType = RecordType.ORDINARY_INCOME_BRACKET,
    ) -> ResolveOut:
        if filing_status is None and taxpayer_class is None:
            raise HTTPException(
                status_code=422,
                detail="provide filing_status or taxpayer_class to identify the chain",
            )
        row = queries.resolve_bracket(
            conn,
            amount=amount,
            tax_year=tax_year,
            jurisdiction=jurisdiction,
            record_type=record_type.value,
            filing_status=None if filing_status is None else filing_status.value,
            taxpayer_class=taxpayer_class,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="no active bracket contains this amount")
        return ResolveOut(amount=amount, record=RecordOut(**row))

    @app.get(
        "/reviews",
        response_model=ReviewsPage,
        summary="Review-queue items (read-only)",
    )
    def read_reviews(
        conn: Conn,
        response: Response,
        status: ReviewQueueStatus | None = None,
        document_id: UUID | None = None,
        cursor: str | None = None,
        limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)] = None,
    ) -> ReviewsPage:
        """Everything the pipeline refused to guess (anti-goal #8), with its
        provenance. Read-only by design: resolving an item is a human
        judgment that this API deliberately does not accept over HTTP."""
        page_size = min(limit or settings.default_page_size, settings.max_page_size)
        rows = queries.list_reviews(
            conn,
            status=None if status is None else status.value,
            document_id=document_id,
            after=None if cursor is None else _decode_cursor(cursor),
            limit=page_size + 1,
        )
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        next_cursor = (
            _encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if has_more and rows else None
        )
        response.headers["Cache-Control"] = "no-store"
        return ReviewsPage(items=[ReviewOut(**row) for row in rows], next_cursor=next_cursor)

    @app.get(
        "/reviews/{review_id}",
        response_model=ReviewOut,
        summary="One review item, with its adjudication audit trail",
    )
    def read_review(review_id: UUID, conn: Conn) -> ReviewOut:
        row = queries.get_review(conn, review_id)
        if row is None:
            raise HTTPException(status_code=404, detail="review item not found")
        return ReviewOut(**row)

    def sweep(limit: Annotated[int, Query(ge=1, le=50)] = 10) -> SweepOut:
        """Two callers, two methods, one handler.

        `POST` is the JobRunner's immediate self-kick after an upload. `GET`
        exists because **Vercel Cron issues GET requests** — a mutating GET
        is not a choice, it is the platform's contract, and the endpoint is
        bearer-authenticated and safe to repeat (`FOR UPDATE SKIP LOCKED`
        means a duplicate sweep claims different jobs, never the same ones
        twice). Vercel injects `Authorization: Bearer $CRON_SECRET`
        automatically when that variable is set, which is exactly the check
        `require_cron_secret` already performs.
        """
        return SweepOut(processed=sweep_pending(settings.database_url, limit=limit))

    # Registered per method with distinct operation ids: one path, one
    # handler, two callers. A single api_route(methods=[...]) would emit a
    # duplicate operationId into the OpenAPI document.
    for method, operation_id in (("POST", "sweep_jobs"), ("GET", "sweep_jobs_cron")):
        app.add_api_route(
            "/internal/sweep",
            sweep,
            methods=[method],
            response_model=SweepOut,
            dependencies=[Depends(require_cron_secret)],
            operation_id=operation_id,
            summary="Process queued jobs (cron / self-kick path)",
        )

    return app
