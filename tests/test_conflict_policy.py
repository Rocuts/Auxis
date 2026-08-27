"""Conflict policy and order-independence, via the repository adapter.

Policy under test (decided at DDL review; first bullet amended 2026-08-27):
- same document re-ingested -> the document's record set is REPLACED, in one
  transaction. This bullet used to read "upsert (idempotent, updated_at
  moves)", which assumed the producer emits the same natural key each run.
  ADR 014 §8 measured that ours does not, and production duplicated 28 of
  document 01's brackets on a single retry. Idempotence now belongs to the
  document, not to a row key the mapper can vary.
- same natural key from a DIFFERENT document -> review queue, never a silent
  overwrite
- lifecycle_status comes from document content, so ingesting document sets in
  any order produces an identical final state (commutativity)
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

import psycopg

from tax_tables.adapters.postgres import PostgresRecordRepository
from tax_tables.domain.records import (
    CanonicalRecord,
    FilingStatus,
    LifecycleStatus,
    RecordType,
)
from tax_tables.ports.repository import DocumentHandle
from tests.conftest import TEST_DSN, reset_database


def _sha(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def _register(repo: PostgresRecordRepository, name: str) -> DocumentHandle:
    return repo.register_document(sha256=_sha(name), filename=name, byte_size=1)


def _deduction(amount: int) -> CanonicalRecord:
    return CanonicalRecord(
        source_page=1,
        table_id="section_1",
        record_type=RecordType.STANDARD_DEDUCTION,
        jurisdiction="US-FED",
        filing_status=FilingStatus.SINGLE,
        tax_year=2026,
        amount=Decimal(amount),
        currency="USD",
        confidence=Decimal(1),
    )


def _bracket(
    lower: int,
    upper: int | None,
    rate: str,
    *,
    lifecycle: LifecycleStatus,
    tax_year: int,
) -> CanonicalRecord:
    return CanonicalRecord(
        source_page=1,
        table_id="table_1",
        record_type=RecordType.PREFERENTIAL_GAIN_BRACKET,
        jurisdiction="US-FED",
        filing_status=FilingStatus.SINGLE,
        tax_year=tax_year,
        lifecycle_status=lifecycle,
        lower_bound=lower,
        upper_bound=upper,
        rate=Decimal(rate),
        currency="USD",
        confidence=Decimal(1),
    )


def test_reupload_of_same_document_is_a_noop(db: psycopg.Connection) -> None:
    with PostgresRecordRepository(TEST_DSN) as repo:
        first = _register(repo, "doc-a.pdf")
        second = _register(repo, "doc-a.pdf")
    assert first.created is True
    assert second.created is False
    assert first.id == second.id


def test_same_document_reingest_upserts(db: psycopg.Connection) -> None:
    with PostgresRecordRepository(TEST_DSN) as repo:
        doc = _register(repo, "doc-a.pdf")
        outcome1 = repo.ingest(doc.id, [_deduction(15400)])
        outcome2 = repo.ingest(doc.id, [_deduction(15500)])  # corrected value

    assert outcome1.persisted == 1 and outcome2.persisted == 1
    assert outcome2.cross_document_conflicts == 0

    row = db.execute("SELECT amount FROM records").fetchall()
    assert len(row) == 1
    assert row[0][0] == Decimal("15500.00")  # refreshed, not duplicated

    # This assertion changed on 2026-08-27 and the reason is worth keeping.
    # It used to read `updated_at > created_at`, pinning re-ingest as a
    # row-level UPSERT. Re-ingest is now a document-scoped REPLACE, so the
    # surviving row is a new row and its timestamps are equal. The observable
    # contract this test exists for — one document, one record set, latest
    # values win, nothing duplicated — is unchanged and still asserted above.
    # Only the mechanism moved, because the upsert relied on the mapper
    # emitting the same natural key twice and it does not (ADR 014 §8).
    fresh = db.execute("SELECT updated_at = created_at FROM records").fetchone()
    assert fresh is not None and fresh[0] is True

    review = db.execute("SELECT count(*) FROM review_queue").fetchone()
    assert review is not None and review[0] == 0


def test_cross_document_conflict_goes_to_review_not_overwrite(
    db: psycopg.Connection,
) -> None:
    with PostgresRecordRepository(TEST_DSN) as repo:
        doc_a = _register(repo, "doc-a.pdf")
        doc_b = _register(repo, "doc-b.pdf")
        repo.ingest(doc_a.id, [_deduction(15400)])
        outcome = repo.ingest(doc_b.id, [_deduction(99999)])

    assert outcome.persisted == 0
    assert outcome.cross_document_conflicts == 1

    # The stored value is untouched and still owned by doc A.
    row = db.execute("SELECT amount, document_id FROM records").fetchall()
    assert len(row) == 1
    assert row[0][0] == Decimal("15400.00")
    assert row[0][1] == doc_a.id

    review = db.execute("SELECT reason, document_id FROM review_queue").fetchall()
    assert len(review) == 1
    assert review[0][0] == "cross_document_natural_key_conflict"
    assert review[0][1] == doc_b.id


def test_overlap_rejection_goes_to_review_and_batch_continues(
    db: psycopg.Connection,
) -> None:
    overlapping = [
        _bracket(0, 48350, "0.0", lifecycle=LifecycleStatus.ACTIVE, tax_year=2026),
        _bracket(40000, None, "0.15", lifecycle=LifecycleStatus.ACTIVE, tax_year=2026),
        _deduction(15400),  # must still land despite the overlap before it
    ]
    with PostgresRecordRepository(TEST_DSN) as repo:
        doc = _register(repo, "doc-a.pdf")
        outcome = repo.ingest(doc.id, overlapping)

    assert outcome.persisted == 2
    assert outcome.overlap_rejections == 1
    review = db.execute("SELECT reason FROM review_queue").fetchall()
    assert [r[0] for r in review] == ["bracket_overlap"]


def _final_state(db: psycopg.Connection) -> list[tuple[object, ...]]:
    return db.execute(
        """
        SELECT d.sha256, r.record_type, r.tax_year, r.filing_status,
               r.lifecycle_status, r.bracket::text, r.rate, r.amount
        FROM records r JOIN documents d ON d.id = r.document_id
        ORDER BY d.sha256, r.record_type, r.tax_year, r.bracket
        """
    ).fetchall()


def test_ingestion_order_is_commutative(db: psycopg.Connection) -> None:
    # Mirrors the corpus shape: an active set and a superseded set covering
    # the same year — document 05's situation. lifecycle_status is declared
    # by content, so arrival order must not change the final state.
    active = [
        _bracket(0, 50000, "0.0", lifecycle=LifecycleStatus.ACTIVE, tax_year=2025),
        _bracket(50001, None, "0.15", lifecycle=LifecycleStatus.ACTIVE, tax_year=2025),
    ]
    superseded = [
        _bracket(0, 48350, "0.0", lifecycle=LifecycleStatus.SUPERSEDED, tax_year=2025),
        _bracket(48351, None, "0.15", lifecycle=LifecycleStatus.SUPERSEDED, tax_year=2025),
    ]

    def run(order: list[tuple[str, list[CanonicalRecord]]]) -> list[tuple[object, ...]]:
        reset_database()
        with PostgresRecordRepository(TEST_DSN) as repo:
            for name, records in order:
                doc = _register(repo, name)
                outcome = repo.ingest(doc.id, records)
                assert outcome.cross_document_conflicts == 0
                assert outcome.overlap_rejections == 0
        with psycopg.connect(TEST_DSN) as conn:
            return _final_state(conn)

    state_ab = run([("new.pdf", active), ("old.pdf", superseded)])
    state_ba = run([("old.pdf", superseded), ("new.pdf", active)])
    assert state_ab == state_ba
    assert len(state_ab) == 4


class TestProvenanceIsBackfilled:
    """`source_kind` is computed on every run and was thrown away on every run.

    The upload path registers the document *before* anything is extracted, so
    it has no `source_kind` and no real filename yet. The pipeline then
    registers the same sha256 again, this time carrying both — and
    `ON CONFLICT (sha256) DO NOTHING` discarded them. There is no
    `UPDATE documents` anywhere in `src/`, so the column was structurally
    always null while `docs/openapi.yaml` published it as *required*, and
    `GET /documents` — the endpoint the README calls "provenance" — returned
    `source_kind: null` and `filename: "upload.pdf"` for all ten live rows.

    That is the extraction router's decision, the thing the "four of five
    documents cost $0" headline rests on, leaving no trace in the data.
    Found by adversarial review, 2026-08-27; nothing had disclosed it.

    Backfill, never overwrite: a value already recorded wins, so a re-ingest
    cannot downgrade known provenance to null.
    """

    def test_second_registration_backfills_source_kind(self, db: psycopg.Connection) -> None:
        """The regression, minimally: register bare, then register enriched."""
        with PostgresRecordRepository(TEST_DSN) as repo:
            first = repo.register_document(
                sha256=_sha("prov.pdf"), filename="upload.pdf", byte_size=10
            )
            second = repo.register_document(
                sha256=_sha("prov.pdf"),
                filename="03_state_local_sales_tax_rates_2026.pdf",
                byte_size=10,
                page_count=2,
                source_kind="digital",
            )
        assert first.id == second.id
        assert second.created is False
        row = db.execute(
            "SELECT source_kind, filename, page_count FROM documents WHERE id = %s",
            (first.id,),
        ).fetchone()
        assert row == ("digital", "03_state_local_sales_tax_rates_2026.pdf", 2)

    def test_a_known_value_is_never_overwritten_by_a_bare_re_register(
        self, db: psycopg.Connection
    ) -> None:
        """Backfill is COALESCE(existing, new), not last-write-wins: a later
        upload that knows nothing must not erase provenance already earned."""
        with PostgresRecordRepository(TEST_DSN) as repo:
            handle = repo.register_document(
                sha256=_sha("keep.pdf"),
                filename="real_name.pdf",
                byte_size=10,
                page_count=7,
                source_kind="scanned",
            )
            repo.register_document(sha256=_sha("keep.pdf"), filename="upload.pdf", byte_size=10)
        row = db.execute(
            "SELECT source_kind, filename, page_count FROM documents WHERE id = %s",
            (handle.id,),
        ).fetchone()
        assert row == ("scanned", "real_name.pdf", 7)
