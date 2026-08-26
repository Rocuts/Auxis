"""Read-side SQL for the API.

The pipeline's driven dependencies go through ports (they must swap per
target); the API's read model is deliberately plain SQL over psycopg — the
same statements serve RDS, Neon, and the local container, so a read port
would be an interface with exactly one implementation. Rows come back as
dicts shaped for the response models.

Cursor pagination is keyset on ``(created_at, id)`` — both immutable — so a
walk is stable under concurrent inserts: no row that existed when the walk
started is skipped or repeated, whatever lands mid-walk. The cursor is the
last row's key, opaque-encoded by the app layer.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

#: ``bracket`` is stored half-open ([lo, hi+1)); the API speaks the canonical
#: inclusive integers, so the upper bound comes back as ``upper - 1``.
_RECORD_COLUMNS = """
    id, document_id, source_page, table_id, record_type, jurisdiction,
    attribute_key, filing_status, taxpayer_class, tax_year,
    effective_from, effective_to, lifecycle_status,
    lower(bracket) AS lower_bound,
    upper(bracket) - 1 AS upper_bound,
    rate, amount, currency, attrs, confidence, review_status,
    created_at, updated_at
"""


def get_document(conn: psycopg.Connection[Any], document_id: UUID) -> dict[str, Any] | None:
    with conn.transaction():
        return (
            conn.cursor(row_factory=dict_row)
            .execute(
                """
            SELECT id, sha256, filename, content_type, byte_size, page_count,
                   source_kind, uploaded_at
            FROM documents WHERE id = %s
            """,
                (document_id,),
            )
            .fetchone()
        )


def list_documents(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    with conn.transaction():
        return (
            conn.cursor(row_factory=dict_row)
            .execute(
                """
            SELECT id, sha256, filename, content_type, byte_size, page_count,
                   source_kind, uploaded_at
            FROM documents ORDER BY uploaded_at, id
            """
            )
            .fetchall()
        )


def get_job(conn: psycopg.Connection[Any], job_id: UUID) -> dict[str, Any] | None:
    with conn.transaction():
        return (
            conn.cursor(row_factory=dict_row)
            .execute(
                """
            SELECT id, document_id, status, attempt, records_extracted,
                   records_persisted, review_count, error, created_at,
                   started_at, finished_at
            FROM jobs WHERE id = %s
            """,
                (job_id,),
            )
            .fetchone()
        )


def list_records(
    conn: psycopg.Connection[Any],
    *,
    tax_year: int | None = None,
    jurisdiction: str | None = None,
    record_type: str | None = None,
    filing_status: str | None = None,
    effective_on: date | None = None,
    include_superseded: bool = False,
    min_confidence: Decimal | None = None,
    after: tuple[datetime, UUID] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """One page of records, keyset-ordered. Returns up to ``limit`` rows;
    the caller derives the next cursor from the last row's
    ``(created_at, id)``.

    ``include_superseded=False`` is the load-bearing default: a
    ``tax_year=2026`` query must not surface document 05's superseded
    records (CLAUDE.md: that is a test, not a claim). ``effective_on``
    means "in force on this date": stated windows must cover it, and an
    unstated bound is unbounded on that side.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if not include_superseded:
        clauses.append("lifecycle_status = 'active'")
    if tax_year is not None:
        clauses.append("tax_year = %s")
        params.append(tax_year)
    if jurisdiction is not None:
        clauses.append("jurisdiction = %s")
        params.append(jurisdiction)
    if record_type is not None:
        clauses.append("record_type = %s")
        params.append(record_type)
    if filing_status is not None:
        clauses.append("filing_status = %s")
        params.append(filing_status)
    if effective_on is not None:
        clauses.append("(effective_from IS NULL OR effective_from <= %s)")
        params.append(effective_on)
        clauses.append("(effective_to IS NULL OR effective_to >= %s)")
        params.append(effective_on)
    if min_confidence is not None:
        clauses.append("confidence >= %s")
        params.append(min_confidence)
    if after is not None:
        clauses.append("(created_at, id) > (%s, %s)")
        params.extend(after)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with conn.transaction():
        return (
            conn.cursor(row_factory=dict_row)
            .execute(
                f"""
            SELECT {_RECORD_COLUMNS}
            FROM records
            {where}
            ORDER BY created_at, id
            LIMIT %s
            """,
                params,
            )
            .fetchall()
        )


def resolve_bracket(
    conn: psycopg.Connection[Any],
    *,
    amount: int,
    tax_year: int,
    jurisdiction: str,
    record_type: str,
    filing_status: str | None,
    taxpayer_class: str | None,
) -> dict[str, Any] | None:
    """The active bracket record containing ``amount`` for the chain.

    The WHERE mirrors the exclusion constraint's expressions — including
    the COALESCE arms — so the same GiST index that makes overlap
    unrepresentable serves this lookup, and the constraint guarantees at
    most one row. This is a data lookup: the response is the bracket
    record, never a computed liability.
    """
    with conn.transaction():
        return (
            conn.cursor(row_factory=dict_row)
            .execute(
                f"""
            SELECT {_RECORD_COLUMNS}
            FROM records
            WHERE jurisdiction = %s
              AND record_type = %s
              AND tax_year = %s
              AND COALESCE(filing_status, '') = COALESCE(%s, '')
              AND COALESCE(taxpayer_class, '') = COALESCE(%s, '')
              AND lifecycle_status = 'active'
              AND bracket @> %s::bigint
            """,
                (jurisdiction, record_type, tax_year, filing_status, taxpayer_class, amount),
            )
            .fetchone()
        )
