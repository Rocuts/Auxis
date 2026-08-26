"""Pipeline composition tests: grid -> mapper -> validators -> persist.

Router and mapper are faked (their own suites cover them); triage and the
Postgres repository are real — these tests run against the docker-compose
database and pin the persistence side of anti-goal #8: everything the
mapper produced is accounted for as a persisted row or a review-queue
entry, never dropped.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg

from tax_tables.adapters.postgres import PostgresRecordRepository
from tax_tables.domain.records import (
    CanonicalRecord,
    FilingStatus,
    RecordType,
    ReviewStatus,
)
from tax_tables.extraction.model import (
    Cell,
    ExtractedDocument,
    ExtractedTable,
    ExtractionCost,
    ExtractionMethod,
    GridSource,
    PageExtraction,
)
from tax_tables.pipeline import run_document
from tax_tables.ports.mapper import MappingIssue, MappingResult
from tests.conftest import TEST_DSN


def _extracted() -> ExtractedDocument:
    table = ExtractedTable(
        page_number=1,
        table_id="p1_t0",
        bbox=(0.0, 0.0, 10.0, 10.0),
        grid_source=GridSource.RULED_LINES,
        rows=[[Cell(text="10%"), Cell(text="$0 - $9,000")]],
        column_count=2,
    )
    return ExtractedDocument(
        filename="doc.pdf",
        sha256="cd" * 32,
        pages=[
            PageExtraction(
                page_number=1,
                width=612.0,
                height=792.0,
                method=ExtractionMethod.DETERMINISTIC_TEXT,
                tables=[table],
            )
        ],
        cost=ExtractionCost(engine="pdfplumber", wall_seconds=0.0),
    )


def _record(lower: int, upper: int | None, **overrides: Any) -> CanonicalRecord:
    values: dict[str, Any] = {
        "source_page": 1,
        "table_id": "p1_t0",
        "record_type": RecordType.ORDINARY_INCOME_BRACKET,
        "jurisdiction": "US",
        "filing_status": FilingStatus.SINGLE,
        "tax_year": 2026,
        "lower_bound": lower,
        "upper_bound": upper,
        "rate": Decimal("0.1"),
        "currency": "USD",
        # A Decimal attr, exactly as the real mapper produces them
        # (parse_float=Decimal): the persistence path must serialize it.
        # (Not a *_rate_pct name — a partial derived triple would rightly
        # draw derived_sum flags and muddy the queue accounting below.)
        "attrs": {"source_table_label": "table_1", "prior_year_amount": Decimal("3.25")},
        "confidence": Decimal("0.95"),
    }
    values.update(overrides)
    return CanonicalRecord(**values)


class _FakeRouter:
    def __init__(self, extracted: ExtractedDocument) -> None:
        self._extracted = extracted

    def extract(self, pdf_bytes: bytes, *, filename: str) -> ExtractedDocument:
        return self._extracted


class _FakeMapper:
    def __init__(self, result: MappingResult) -> None:
        self._result = result

    def map_document(self, extracted: ExtractedDocument) -> MappingResult:
        return self._result


def _mapping() -> MappingResult:
    return MappingResult(
        records=[
            _record(0, 9000),
            _record(9001, None),
            # Overlaps the first record's interval: triage must REJECT it
            # into the review queue, not hand it to the database.
            _record(500, 8000, rate=Decimal("0.2")),
        ],
        issues=[
            MappingIssue(
                source_page=1,
                table_id="p1_t0",
                row_index=0,
                col_index=1,
                raw_value="??",
                reason="unreadable cell",
            )
        ],
    )


class TestRunDocument:
    def test_persists_and_queues_everything(self, db: psycopg.Connection) -> None:
        with PostgresRecordRepository(TEST_DSN) as repository:
            result = run_document(
                b"%PDF-fake",
                filename="doc.pdf",
                router=_FakeRouter(_extracted()),
                mapper=_FakeMapper(_mapping()),
                repository=repository,
            )

        assert result.document_id is not None
        assert result.ingest is not None
        assert result.ingest.persisted == 2

        rows = db.execute("SELECT count(*) FROM records").fetchone()
        assert rows is not None and rows[0] == 2
        # The Decimal attr survived JSONB serialization with its digits.
        stored = db.execute(
            "SELECT attrs->>'prior_year_amount' FROM records WHERE lower(bracket) = 0"
        ).fetchone()
        assert stored is not None and Decimal(stored[0]) == Decimal("3.25")
        # Every finding reaches the queue with its reason: the rejected
        # record's two (overlap + the gap it creates), the persisted
        # neighbour's FLAG (a needs_review row without its why would be
        # useless to a reviewer), plus the mapper's own issue.
        queue = db.execute("SELECT reason FROM review_queue ORDER BY reason").fetchall()
        reasons = [row[0] for row in queue]
        assert len(reasons) == 4
        assert any(reason.startswith("bracket_overlap:") for reason in reasons)
        assert sum(reason.startswith("bracket_gap:") for reason in reasons) == 2
        assert "mapping: unreadable cell" in reasons

    def test_dry_run_without_repository(self) -> None:
        result = run_document(
            b"%PDF-fake",
            filename="doc.pdf",
            router=_FakeRouter(_extracted()),
            mapper=_FakeMapper(_mapping()),
        )
        assert result.document_id is None
        assert result.ingest is None
        assert len(result.triage.persistable) == 2
        assert len(result.triage.rejected) == 1

    def test_flagged_record_persists_as_needs_review(self, db: psycopg.Connection) -> None:
        mapping = MappingResult(records=[_record(0, 9000, confidence=Decimal("0.5"))], issues=[])
        with PostgresRecordRepository(TEST_DSN) as repository:
            run_document(
                b"%PDF-fake",
                filename="doc.pdf",
                router=_FakeRouter(_extracted()),
                mapper=_FakeMapper(mapping),
                repository=repository,
            )
        row = db.execute("SELECT review_status FROM records").fetchone()
        assert row is not None and row[0] == ReviewStatus.NEEDS_REVIEW.value
        # The reason the record needs review is queued alongside it.
        reason = db.execute("SELECT reason FROM review_queue").fetchone()
        assert reason is not None and reason[0].startswith("confidence_floor:")


class TestQueueReview:
    def test_inserts_entries_with_provenance(self, db: psycopg.Connection) -> None:
        with PostgresRecordRepository(TEST_DSN) as repository:
            handle = repository.register_document(
                sha256="ef" * 32, filename="doc.pdf", byte_size=10
            )
            inserted = repository.queue_review(
                handle.id,
                [
                    {
                        "source_page": 2,
                        "table_id": "p2_t1",
                        "row_index": 3,
                        "col_index": 0,
                        "raw_value": "5..2%",
                        "reason": "mapping: malformed rate",
                    }
                ],
            )
        assert inserted == 1
        row = db.execute(
            "SELECT source_page, table_id, row_index, col_index, raw_value, reason"
            " FROM review_queue"
        ).fetchone()
        assert row == (2, "p2_t1", 3, 0, "5..2%", "mapping: malformed rate")
