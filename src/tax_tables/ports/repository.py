"""RecordRepository port.

Three adapters implement it (psycopg -> RDS Proxy, psycopg -> Neon,
psycopg -> local container) — the SQL is identical, only the DSN differs,
which is why the port is one class deep.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from tax_tables.domain.records import CanonicalRecord


@dataclass(frozen=True)
class DocumentHandle:
    id: UUID
    # False when this sha256 was already registered: re-uploading the same
    # PDF is a no-op (idempotency level 1).
    created: bool


@dataclass(frozen=True)
class IngestOutcome:
    # Rows inserted or refreshed via the natural-key upsert (same document).
    persisted: int
    # Natural-key conflicts against a DIFFERENT document — routed to the
    # review queue, never silently overwritten.
    cross_document_conflicts: int
    # Bracket-overlap rejections raised by the exclusion constraint —
    # also routed to the review queue.
    overlap_rejections: int


class RecordRepository(Protocol):
    def register_document(
        self,
        *,
        sha256: str,
        filename: str,
        byte_size: int,
        content_type: str = "application/pdf",
        page_count: int | None = None,
        source_kind: str | None = None,
    ) -> DocumentHandle: ...

    def ingest(self, document_id: UUID, records: Sequence[CanonicalRecord]) -> IngestOutcome: ...

    def queue_review(self, document_id: UUID, entries: Sequence[Mapping[str, Any]]) -> int:
        """Insert review-queue entries (mapping issues, triage rejections).

        Each entry carries the ``review_queue`` column values: source_page,
        table_id, row_index, col_index, raw_value, reason. Returns how many
        were inserted.
        """
        ...
