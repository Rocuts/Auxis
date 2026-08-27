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
from tax_tables.ports.adjudicator import ReviewItem


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

    def record_present(self, document_id: UUID, record: CanonicalRecord) -> bool:
        """Is this exact record in the fact table, under THIS document?

        The question the adjudicator's auto-resolve gate actually needs to
        ask. Matching is on the ``records_natural_key`` columns with the
        constraint's own NULLS-NOT-DISTINCT semantics, so a scalar record
        (bracket NULL) matches another scalar record rather than nothing.

        Scoped to ``document_id`` deliberately. A
        ``cross_document_natural_key_conflict`` means the key IS present —
        held by a DIFFERENT document, which is precisely why this document's
        record was refused. An unscoped lookup would answer "present" and
        licence closing the row that stands for the loss.
        """
        ...

    def queue_review(self, document_id: UUID, entries: Sequence[Mapping[str, Any]]) -> int:
        """Insert review-queue entries (mapping issues, triage rejections).

        Each entry carries the ``review_queue`` column values: source_page,
        table_id, row_index, col_index, raw_value, reason. Returns how many
        were inserted.
        """
        ...

    def list_open_reviews(self, document_id: UUID) -> list[ReviewItem]:
        """The adjudicator's WORK list: ``status='open'`` rows that carry no
        stored proposal yet. An item a previous pass already proposed on
        awaits its human and is excluded — re-ingesting a document never
        re-pays for it. Ordered by creation time then id; ties inside one
        transaction timestamp fall back to id order, so strict insertion
        order is not guaranteed within a batch."""
        ...

    def resolve_review(
        self, item_id: UUID, *, resolution: Mapping[str, Any], resolved_by: str
    ) -> None:
        """Mark one open item resolved with its full audit trail (resolution
        payload, who, when — migration 0007's CHECK makes a resolved row
        without the trail unrepresentable). Raises ``ValueError`` if the item
        is not open: silently re-resolving would overwrite an audit record.
        """
        ...

    def propose_resolution(self, item_id: UUID, proposal: Mapping[str, Any]) -> None:
        """Store a below-threshold adjudication on an item that STAYS open —
        the human reviewer sees the proposal; nothing is resolved."""
        ...
