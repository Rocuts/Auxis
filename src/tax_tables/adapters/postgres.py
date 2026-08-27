"""Postgres adapter for RecordRepository (psycopg 3).

The same code serves all three targets — RDS Proxy, Neon pooled endpoint,
local container — so pooler compatibility is handled here once:

- psycopg >= 3.2 speaks protocol-level prepared statements through
  PgBouncer >= 1.22 (Neon's pooler) when the bundled libpq is >= 17. We gate
  on the capability instead of blanket-disabling prepared statements — the
  2024-era `prepare_threshold=None` habit costs real throughput. On an old
  libpq the documented fallback is to prepare but never deallocate.

Conflict policy (decided at DDL review, tested in test_conflict_policy.py):

- Same document re-ingested -> natural-key upsert refreshes the row
  (updated_at moves). Idempotency level 2.
- Same natural key from a DIFFERENT document -> the incoming record goes to
  the review queue with its provenance; the stored row is never silently
  overwritten (anti-goal #8).
- Bracket overlap -> rejected by the exclusion constraint, routed to the
  review queue; the rest of the batch continues (per-record savepoints).
- lifecycle_status arrives on the record, declared by document content; the
  repository never promotes or demotes rows as a side effect of an insert,
  which is what makes ingestion order-independent.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from functools import partial
from types import TracebackType
from typing import Any
from uuid import UUID

import psycopg
from psycopg import errors
from psycopg.types.json import Jsonb

from tax_tables.domain.records import CanonicalRecord
from tax_tables.ports.adjudicator import ReviewItem
from tax_tables.ports.repository import DocumentHandle, IngestOutcome

#: The mapper produces Decimal attr values (its JSON is parsed with
#: parse_float=Decimal — no float ever touches a value), and the stdlib
#: dumper behind Jsonb cannot serialize Decimal. default=str keeps the
#: exact digits ("3.25" stays "3.25"); the validators' _attr_decimal reads
#: either representation back losslessly.
_JSONB_DUMPS = partial(json.dumps, sort_keys=True, default=str)

_INSERT_RECORD = """
INSERT INTO records (
    document_id, source_page, table_id,
    tax_year, effective_from, effective_to, lifecycle_status,
    jurisdiction, record_type, attribute_key, filing_status, taxpayer_class,
    bracket, rate, amount, currency, attrs, confidence, review_status
) VALUES (
    %(document_id)s, %(source_page)s, %(table_id)s,
    %(tax_year)s, %(effective_from)s, %(effective_to)s, %(lifecycle_status)s,
    %(jurisdiction)s, %(record_type)s, %(attribute_key)s, %(filing_status)s,
    %(taxpayer_class)s,
    CASE WHEN %(lower_bound)s::bigint IS NULL THEN NULL
         ELSE int8range(%(lower_bound)s::bigint, %(upper_bound)s::bigint, '[]')
    END,
    %(rate)s, %(amount)s, %(currency)s, %(attrs)s, %(confidence)s,
    %(review_status)s
)
ON CONFLICT ON CONSTRAINT records_natural_key DO UPDATE SET
    source_page   = EXCLUDED.source_page,
    table_id      = EXCLUDED.table_id,
    effective_from = EXCLUDED.effective_from,
    effective_to  = EXCLUDED.effective_to,
    rate          = EXCLUDED.rate,
    amount        = EXCLUDED.amount,
    currency      = EXCLUDED.currency,
    attrs         = EXCLUDED.attrs,
    confidence    = EXCLUDED.confidence,
    review_status = EXCLUDED.review_status,
    updated_at    = now()
WHERE records.document_id = EXCLUDED.document_id
RETURNING id
"""

_INSERT_REVIEW = """
INSERT INTO review_queue (document_id, source_page, table_id, raw_value, reason)
VALUES (%(document_id)s, %(source_page)s, %(table_id)s, %(raw_value)s, %(reason)s)
"""

#: Presence of one record under one document, on the records_natural_key
#: columns. IS NOT DISTINCT FROM mirrors the constraint's NULLS NOT
#: DISTINCT: plain "=" would make every NULL discriminator un-findable, so a
#: scalar record would read as absent and its queue row would never close.
#: The bracket is rebuilt exactly as _INSERT_RECORD builds it, inclusive on
#: both ends, so an open top (upper NULL) matches an open top.
#: Idempotence is document-scoped: a re-ingest replaces this document's set
#: rather than trusting a row-level key whose columns a stochastic mapper is
#: free to vary between runs. See PostgresRecordRepository.ingest.
_DELETE_DOCUMENT_RECORDS = """
    DELETE FROM records WHERE document_id = %(document_id)s
"""

#: Only the superseded run's untouched work items. A row that carries a
#: resolution or a stored adjudicator proposal, or that a human already
#: resolved or dismissed, is audit history — it is never deleted.
_DELETE_DOCUMENT_OPEN_REVIEWS = """
    DELETE FROM review_queue
    WHERE document_id = %(document_id)s
      AND status = 'open'
      AND resolution IS NULL
"""

_SELECT_RECORD_PRESENT = """
SELECT 1 FROM records
WHERE document_id = %(document_id)s
  AND jurisdiction = %(jurisdiction)s
  AND record_type = %(record_type)s
  AND attribute_key IS NOT DISTINCT FROM %(attribute_key)s
  AND tax_year IS NOT DISTINCT FROM %(tax_year)s
  AND filing_status IS NOT DISTINCT FROM %(filing_status)s
  AND taxpayer_class IS NOT DISTINCT FROM %(taxpayer_class)s
  AND lifecycle_status = %(lifecycle_status)s
  AND bracket IS NOT DISTINCT FROM
      CASE WHEN %(lower_bound)s::bigint IS NULL THEN NULL
           ELSE int8range(%(lower_bound)s::bigint, %(upper_bound)s::bigint, '[]')
      END
LIMIT 1
"""

_INSERT_REVIEW_ENTRY = """
INSERT INTO review_queue
    (document_id, source_page, table_id, row_index, col_index, raw_value, reason)
VALUES (%(document_id)s, %(source_page)s, %(table_id)s, %(row_index)s, %(col_index)s,
        %(raw_value)s, %(reason)s)
"""


class PostgresRecordRepository:
    def __init__(self, conninfo: str) -> None:
        self._conn: psycopg.Connection[Any] = psycopg.connect(conninfo, connect_timeout=30)
        self._owns_conn = True
        if not psycopg.capabilities.has_send_close_prepared():
            # Old libpq behind a transaction pooler: keep preparing, never
            # send DEALLOCATE (which SQL-level poolers reject).
            self._conn.prepared_max = None

    @classmethod
    def from_connection(cls, conn: psycopg.Connection[Any]) -> PostgresRecordRepository:
        """Wrap an existing connection (e.g. the API's request-scoped one).
        The caller keeps ownership: ``close()`` on this instance is a no-op."""
        repository = cls.__new__(cls)
        repository._conn = conn
        repository._owns_conn = False
        return repository

    def close(self) -> None:
        if self._owns_conn:
            self._conn.close()

    @property
    def connection(self) -> psycopg.Connection[Any]:
        """The underlying connection, for same-database service SQL (the
        jobs table) that must share this adapter's transactional context."""
        return self._conn

    def __enter__(self) -> PostgresRecordRepository:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def register_document(
        self,
        *,
        sha256: str,
        filename: str,
        byte_size: int,
        content_type: str = "application/pdf",
        page_count: int | None = None,
        source_kind: str | None = None,
    ) -> DocumentHandle:
        with self._conn.transaction():
            row = self._conn.execute(
                """
                INSERT INTO documents
                    (sha256, filename, byte_size, content_type, page_count, source_kind)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (sha256) DO NOTHING
                RETURNING id
                """,
                (sha256, filename, byte_size, content_type, page_count, source_kind),
            ).fetchone()
            if row is not None:
                return DocumentHandle(id=row[0], created=True)
            existing = self._conn.execute(
                "SELECT id FROM documents WHERE sha256 = %s", (sha256,)
            ).fetchone()
            assert existing is not None  # unique key: insert lost means row exists
            return DocumentHandle(id=existing[0], created=False)

    def ingest(self, document_id: UUID, records: Sequence[CanonicalRecord]) -> IngestOutcome:
        """Replace this document's records with ``records``, atomically.

        **Why a delete and not only an upsert.** The natural key upsert makes
        a re-ingest idempotent *for a deterministic producer*. Ours is not one
        (ADR 014 §8): the mapper varies convention fields like
        ``taxpayer_class`` between runs, and two runs of one document then
        write two genuinely distinct natural keys for the same real bracket.
        Nothing collides, nothing is refused, and the document quietly holds
        both. Production did exactly this on 2026-08-27 — 60 rows for a
        32-record document after one lease reclaim.

        So idempotence is enforced at the level that actually owns the
        invariant: **one document, one record set.** The delete and the
        inserts share a single transaction, because a worker killed between
        them would leave the document with no records at all — duplicated
        data traded for lost data, which is the worse failure (anti-goal #8).

        Scope is exactly this ``document_id``. Cross-document supersession and
        the cross-document natural-key conflict policy are a different
        mechanism and are untouched.
        """
        persisted = 0
        conflicts = 0
        overlaps = 0
        with self._conn.transaction():
            self._conn.execute(_DELETE_DOCUMENT_RECORDS, {"document_id": document_id})
            # Open, un-adjudicated review items for this document belong to
            # the superseded run and would accumulate in the same way. Rows
            # carrying a resolution, a stored proposal, or a terminal status
            # are audit history and are never deleted.
            self._conn.execute(_DELETE_DOCUMENT_OPEN_REVIEWS, {"document_id": document_id})
            for record in records:
                params = _record_params(document_id, record)
                try:
                    with self._conn.transaction():  # savepoint per record
                        row = self._conn.execute(_INSERT_RECORD, params).fetchone()
                except errors.ExclusionViolation:
                    overlaps += 1
                    self._queue_for_review(document_id, record, "bracket_overlap")
                    continue
                if row is None:
                    # Natural key held by another document: DO UPDATE's WHERE
                    # clause refused the overwrite.
                    conflicts += 1
                    self._queue_for_review(
                        document_id, record, "cross_document_natural_key_conflict"
                    )
                else:
                    persisted += 1
        return IngestOutcome(
            persisted=persisted,
            cross_document_conflicts=conflicts,
            overlap_rejections=overlaps,
        )

    def record_present(self, document_id: UUID, record: CanonicalRecord) -> bool:
        """Whether this record reached the fact table under this document."""
        with self._conn.transaction():
            row = self._conn.execute(
                _SELECT_RECORD_PRESENT, _record_params(document_id, record)
            ).fetchone()
        return row is not None

    def queue_review(self, document_id: UUID, entries: Sequence[Mapping[str, Any]]) -> int:
        """Insert pipeline-produced review entries (mapping issues, triage
        rejections) with their cell/record provenance."""
        inserted = 0
        with self._conn.transaction():
            for entry in entries:
                self._conn.execute(
                    _INSERT_REVIEW_ENTRY,
                    {
                        "document_id": document_id,
                        "source_page": entry.get("source_page"),
                        "table_id": entry.get("table_id"),
                        "row_index": entry.get("row_index"),
                        "col_index": entry.get("col_index"),
                        "raw_value": entry.get("raw_value"),
                        "reason": entry["reason"],
                    },
                )
                inserted += 1
        return inserted

    def list_open_reviews(self, document_id: UUID) -> list[ReviewItem]:
        # transaction() even for a read: a bare execute would open psycopg's
        # implicit transaction and silently demote every later transaction()
        # to a savepoint inside it — rolled back, not committed, on close.
        # resolution IS NULL keeps this a WORK list: an item already carrying
        # a stored proposal awaits its human and is never re-adjudicated, so
        # re-ingesting a document does not re-pay for old open items.
        with self._conn.transaction():
            rows = self._conn.execute(
                """
                SELECT id, document_id, source_page, table_id, row_index, col_index,
                       raw_value, reason
                FROM review_queue
                WHERE document_id = %s AND status = 'open' AND resolution IS NULL
                ORDER BY created_at, id
                """,
                (document_id,),
            ).fetchall()
        return [
            ReviewItem(
                id=row[0],
                document_id=row[1],
                source_page=row[2],
                table_id=row[3],
                row_index=row[4],
                col_index=row[5],
                raw_value=row[6],
                reason=row[7],
            )
            for row in rows
        ]

    def resolve_review(
        self, item_id: UUID, *, resolution: Mapping[str, Any], resolved_by: str
    ) -> None:
        with self._conn.transaction():
            cursor = self._conn.execute(
                """
                UPDATE review_queue
                SET status = 'resolved', resolution = %s,
                    resolved_by = %s, resolved_at = now()
                WHERE id = %s AND status = 'open'
                """,
                (Jsonb(dict(resolution), dumps=_JSONB_DUMPS), resolved_by, item_id),
            )
            if cursor.rowcount != 1:
                # Re-resolving would silently overwrite an audit record.
                raise ValueError(f"review item {item_id} is not open")

    def propose_resolution(self, item_id: UUID, proposal: Mapping[str, Any]) -> None:
        with self._conn.transaction():
            cursor = self._conn.execute(
                "UPDATE review_queue SET resolution = %s WHERE id = %s AND status = 'open'",
                (Jsonb(dict(proposal), dumps=_JSONB_DUMPS), item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"review item {item_id} is not open")

    # -- BlobStore port (same database, same connection) ------------------

    def store_blob(self, document_id: UUID, content: bytes) -> None:
        with self._conn.transaction():
            self._conn.execute(
                """
                INSERT INTO document_blobs (document_id, content)
                VALUES (%s, %s)
                ON CONFLICT (document_id) DO UPDATE SET content = EXCLUDED.content
                """,
                (document_id, content),
            )

    def load_blob(self, document_id: UUID) -> bytes:
        with self._conn.transaction():
            row = self._conn.execute(
                "SELECT content FROM document_blobs WHERE document_id = %s", (document_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"document {document_id} has no stored blob")
        return bytes(row[0])

    def _queue_for_review(self, document_id: UUID, record: CanonicalRecord, reason: str) -> None:
        self._conn.execute(
            _INSERT_REVIEW,
            {
                "document_id": document_id,
                "source_page": record.source_page,
                "table_id": record.table_id,
                "raw_value": json.dumps(record.model_dump(mode="json"), sort_keys=True),
                "reason": reason,
            },
        )


def _record_params(document_id: UUID, record: CanonicalRecord) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "source_page": record.source_page,
        "table_id": record.table_id,
        "tax_year": record.tax_year,
        "effective_from": record.effective_from,
        "effective_to": record.effective_to,
        "lifecycle_status": record.lifecycle_status.value,
        "jurisdiction": record.jurisdiction,
        "record_type": record.record_type.value,
        "attribute_key": record.attribute_key,
        "filing_status": (record.filing_status.value if record.filing_status is not None else None),
        "taxpayer_class": record.taxpayer_class,
        "lower_bound": record.lower_bound,
        "upper_bound": record.upper_bound,
        "rate": record.rate,
        "amount": record.amount,
        "currency": record.currency,
        "attrs": Jsonb(record.attrs, dumps=_JSONB_DUMPS),
        "confidence": record.confidence,
        "review_status": record.review_status.value,
    }
