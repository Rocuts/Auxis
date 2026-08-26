"""Pipeline composition tests: grid -> mapper -> validators -> persist.

Router and mapper are faked (their own suites cover them); triage and the
Postgres repository are real — these tests run against the docker-compose
database and pin the persistence side of anti-goal #8: everything the
mapper produced is accounted for as a persisted row or a review-queue
entry, never dropped.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

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
    ProseBlock,
    ProseKind,
)
from tax_tables.pipeline import run_document
from tax_tables.ports.adjudicator import (
    Adjudication,
    AdjudicationError,
    Adjudicator,
    ReviewItem,
)
from tax_tables.ports.mapper import MappingCost, MappingIssue, MappingResult
from tax_tables.ports.repository import DocumentHandle, IngestOutcome
from tax_tables.ports.verifier import RecordVerdict, Verdict, VerificationResult
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


# ---------------------------------------------------------------------------
# Semantic-layer wiring (ADR 012): verifier disputes and the adjudicator pass
# ---------------------------------------------------------------------------


class _FakeVerifier:
    def __init__(self, result: VerificationResult) -> None:
        self._result = result
        self.calls: list[MappingResult] = []

    def verify(self, extracted: ExtractedDocument, mapping: MappingResult) -> VerificationResult:
        self.calls.append(mapping)
        return self._result


def _verdicts(*entries: tuple[str, str | None]) -> VerificationResult:
    return VerificationResult(
        verdicts=[
            RecordVerdict(record_index=index, verdict=Verdict(kind), reason=reason)
            for index, (kind, reason) in enumerate(entries)
        ]
    )


def _stamped_scan_extracted() -> ExtractedDocument:
    """The stamped-scan shape from the 2a router review: a scanned page whose
    only text layer is a records-management stamp, OCR-extracted with one
    doubtful cell, and a footnote that settles the doubtful value."""
    table = ExtractedTable(
        page_number=1,
        table_id="p1_t0",
        bbox=(0.0, 0.0, 500.0, 300.0),
        grid_source=GridSource.RULED_CELL_OCR,
        rows=[
            [Cell(text="Rate"), Cell(text="Single")],
            [Cell(text="10%", confidence=Decimal("0.55")), Cell(text="$0 - $9,000")],
        ],
        column_count=2,
    )
    stamp = ProseBlock(
        page_number=1,
        kind=ProseKind.HEADING,
        text="Received by Records Management on 14 March 2026 Bates 000147",
        bbox=(0.0, 310.0, 500.0, 322.0),
        confidence=Decimal("0.99"),
    )
    footnote = ProseBlock(
        page_number=1,
        kind=ProseKind.FOOTNOTE,
        text="NOTE. The first-bracket rate is 10 percent.",
        bbox=(0.0, 330.0, 500.0, 342.0),
        confidence=Decimal("0.97"),
    )
    page = PageExtraction(
        page_number=1,
        width=612.0,
        height=792.0,
        method=ExtractionMethod.OCR,
        tables=[table],
        prose=[stamp, footnote],
    )
    return ExtractedDocument(
        filename="stamped_scan.pdf",
        sha256="ab" * 32,
        pages=[page],
        cost=ExtractionCost(engine="tesseract", wall_seconds=1.0),
    )


class _FakeAdjudicator(Adjudicator):
    """Keys behavior on the queued reason: auto-resolve, propose, or fail."""

    def __init__(self) -> None:
        self.seen: list[ReviewItem] = []

    def adjudicate(self, item: ReviewItem, extracted: ExtractedDocument) -> Adjudication:
        self.seen.append(item)
        if item.reason.startswith("mapping:"):
            raise AdjudicationError("adjudication call refused")
        cost = MappingCost(engine="test-model", api_calls=1)
        if item.reason.startswith("confidence_floor:"):
            # The footnote settles the doubtful OCR cell: high confidence,
            # citations into the extracted document.
            return Adjudication(
                item_id=item.id,
                resolution="footnote p1 states 10 percent; the mapped 0.10 is correct",
                citations=[
                    {
                        "kind": "prose",
                        "page": 1,
                        "table_id": None,
                        "row": None,
                        "col": None,
                        "prose_index": 1,
                    }
                ],
                confidence=Decimal("0.97"),
                citations_valid=True,
                cost=cost,
            )
        return Adjudication(
            item_id=item.id,
            resolution="evidence does not settle the dispute; needs a human",
            citations=[],
            confidence=Decimal("0.60"),
            citations_valid=False,
            cost=cost,
        )


class TestVerifierWiring:
    def test_dispute_persists_as_needs_review_and_queues_the_reason(
        self, db: psycopg.Connection
    ) -> None:
        mapping = MappingResult(records=[_record(0, 9000), _record(9001, None)], issues=[])
        verifier = _FakeVerifier(
            _verdicts(("confirmed", None), ("disputed", "cell 1,1 does not support the bound"))
        )
        with PostgresRecordRepository(TEST_DSN) as repository:
            result = run_document(
                b"%PDF-fake",
                filename="doc.pdf",
                router=_FakeRouter(_extracted()),
                mapper=_FakeMapper(mapping),
                verifier=verifier,
                repository=repository,
            )

        assert result.verification is not None
        assert len(result.verification.disputed) == 1
        assert verifier.calls == [mapping]  # the verifier saw the raw mapping
        # The disputed record still persists — flagged, never dropped.
        assert result.ingest is not None and result.ingest.persisted == 2
        statuses: dict[int, str] = dict(
            db.execute("SELECT lower(bracket), review_status FROM records").fetchall()
        )
        assert statuses[0] == ReviewStatus.CLEAN.value
        assert statuses[9001] == ReviewStatus.NEEDS_REVIEW.value
        reasons = [row[0] for row in db.execute("SELECT reason FROM review_queue").fetchall()]
        assert reasons == ["verifier_dispute: cell 1,1 does not support the bound"]

    def test_without_a_verifier_verification_is_none(self) -> None:
        result = run_document(
            b"%PDF-fake",
            filename="doc.pdf",
            router=_FakeRouter(_extracted()),
            mapper=_FakeMapper(_mapping()),
        )
        assert result.verification is None
        assert result.adjudications == []

    def test_dispute_joins_a_rejected_records_findings_dry(self) -> None:
        # Index 2 overlaps (rejected); the dispute must ride along, not vanish.
        verifier = _FakeVerifier(
            _verdicts(
                ("confirmed", None),
                ("confirmed", None),
                ("disputed", "rate not supported by any cited cell"),
            )
        )
        result = run_document(
            b"%PDF-fake",
            filename="doc.pdf",
            router=_FakeRouter(_extracted()),
            mapper=_FakeMapper(_mapping()),
            verifier=verifier,
        )
        (rejected,) = result.triage.rejected
        assert rejected.index == 2
        assert {f.rule for f in rejected.findings} >= {"bracket_overlap", "verifier_dispute"}


class TestAdjudicatorPass:
    def _run(self, repository: PostgresRecordRepository) -> tuple[_FakeAdjudicator, list[str]]:
        # Three queue entries: a verifier dispute (stays human), a
        # confidence-floor flag (the footnote settles it: auto-resolves),
        # and a mapper issue (the adjudication call fails).
        mapping = MappingResult(
            records=[
                _record(0, 9000),
                _record(9001, None, confidence=Decimal("0.5")),
            ],
            issues=[
                MappingIssue(
                    source_page=1,
                    table_id="p1_t0",
                    row_index=1,
                    col_index=0,
                    raw_value="1O%",
                    reason="OCR-doubtful rate glyphs",
                )
            ],
        )
        verifier = _FakeVerifier(
            _verdicts(("disputed", "stamp text overlays the header band"), ("confirmed", None))
        )
        adjudicator = _FakeAdjudicator()
        result = run_document(
            b"%PDF-fake",
            filename="stamped_scan.pdf",
            router=_FakeRouter(_stamped_scan_extracted()),
            mapper=_FakeMapper(mapping),
            verifier=verifier,
            repository=repository,
            adjudicator=adjudicator,
        )
        return adjudicator, [outcome.disposition for outcome in result.adjudications]

    def test_threshold_splits_resolve_proposal_and_error(self, db: psycopg.Connection) -> None:
        with PostgresRecordRepository(TEST_DSN) as repository:
            adjudicator, dispositions = self._run(repository)
            open_after = repository.list_open_reviews(
                repository.register_document(
                    sha256="ab" * 32, filename="stamped_scan.pdf", byte_size=9
                ).id
            )

        assert len(adjudicator.seen) == 3  # every open item was examined
        assert sorted(dispositions) == ["auto_resolved", "error", "proposal_stored"]

        rows = db.execute(
            "SELECT reason, status, resolution, resolved_by,"
            "       resolved_at IS NOT NULL FROM review_queue"
        ).fetchall()
        by_reason = {row[0].split(":")[0]: row[1:] for row in rows}
        status, resolution, resolved_by, has_time = by_reason["confidence_floor"]
        assert status == "resolved"
        assert resolution["confidence"] == "0.97"
        assert resolution["citations"][0]["prose_index"] == 1
        assert resolved_by == "adjudicator:test-model"
        assert has_time
        status, resolution, resolved_by, has_time = by_reason["verifier_dispute"]
        assert status == "open"  # proposal stored, item stays human
        assert resolution["citations_valid"] is False
        assert resolved_by is None and not has_time
        status, resolution, resolved_by, has_time = by_reason["mapping"]
        assert status == "open" and resolution is None  # failed call touches nothing
        # Work list after the pass: the resolved item is closed and the
        # proposal-stored item awaits its human — only the errored item
        # (no proposal) remains adjudicable.
        assert len(open_after) == 1

    def test_dry_run_never_adjudicates(self) -> None:
        adjudicator = _FakeAdjudicator()
        result = run_document(
            b"%PDF-fake",
            filename="doc.pdf",
            router=_FakeRouter(_extracted()),
            mapper=_FakeMapper(_mapping()),
            adjudicator=adjudicator,
        )
        assert adjudicator.seen == []
        assert result.adjudications == []


# ---------------------------------------------------------------------------
# Adjudicator-pass containment and eligibility (adversarial-review fixes):
# an in-memory repository lets these pin races and non-port exceptions that
# the real database cannot stage deterministically.
# ---------------------------------------------------------------------------


class _MemoryRepository:
    """RecordRepository fake, faithful to the port for the adjudication path:
    resolve/propose raise ValueError on a not-open item, exactly like the
    Postgres adapter."""

    def __init__(self) -> None:
        self.status: dict[UUID, str] = {}
        self.items: list[ReviewItem] = []
        self.proposals: dict[UUID, dict[str, Any]] = {}
        self.resolutions: dict[UUID, tuple[dict[str, Any], str]] = {}
        self.fail_resolves: bool = False

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
        self.document_id = uuid4()
        return DocumentHandle(id=self.document_id, created=True)

    def ingest(self, document_id: UUID, records: Sequence[CanonicalRecord]) -> IngestOutcome:
        return IngestOutcome(
            persisted=len(records), cross_document_conflicts=0, overlap_rejections=0
        )

    def queue_review(self, document_id: UUID, entries: Sequence[Mapping[str, Any]]) -> int:
        for entry in entries:
            item = ReviewItem(
                id=uuid4(),
                document_id=document_id,
                source_page=entry.get("source_page"),
                table_id=entry.get("table_id"),
                row_index=entry.get("row_index"),
                col_index=entry.get("col_index"),
                raw_value=entry.get("raw_value"),
                reason=str(entry["reason"]),
            )
            self.items.append(item)
            self.status[item.id] = "open"
        return len(entries)

    def list_open_reviews(self, document_id: UUID) -> list[ReviewItem]:
        # Work-list semantics, like the Postgres adapter: open AND not yet
        # carrying a stored proposal.
        return [
            item
            for item in self.items
            if self.status[item.id] == "open" and item.id not in self.proposals
        ]

    def resolve_review(
        self, item_id: UUID, *, resolution: Mapping[str, Any], resolved_by: str
    ) -> None:
        if self.fail_resolves or self.status.get(item_id) != "open":
            raise ValueError(f"review item {item_id} is not open")
        self.status[item_id] = "resolved"
        self.resolutions[item_id] = (dict(resolution), resolved_by)

    def propose_resolution(self, item_id: UUID, proposal: Mapping[str, Any]) -> None:
        if self.status.get(item_id) != "open":
            raise ValueError(f"review item {item_id} is not open")
        self.proposals[item_id] = dict(proposal)


class _ConfidentAdjudicator:
    """Returns a high-confidence, validly-cited adjudication for EVERY item —
    the eligibility gate, not the adjudication, must hold the line."""

    def adjudicate(self, item: ReviewItem, extracted: ExtractedDocument) -> Adjudication:
        return Adjudication(
            item_id=item.id,
            resolution="the page settles this item",
            citations=[
                {
                    "kind": "prose",
                    "page": 1,
                    "table_id": None,
                    "row": None,
                    "col": None,
                    "prose_index": 1,
                }
            ],
            confidence=Decimal("0.97"),
            citations_valid=True,
            cost=MappingCost(engine="test-model", api_calls=1),
        )


class _TransportFailingAdjudicator(_ConfidentAdjudicator):
    """Raises a NON-port exception (an SDK transport failure stand-in) for
    one designated reason prefix; adjudicates the rest normally."""

    def __init__(self, failing_prefix: str) -> None:
        self._prefix = failing_prefix

    def adjudicate(self, item: ReviewItem, extracted: ExtractedDocument) -> Adjudication:
        if item.reason.startswith(self._prefix):
            raise ConnectionError("simulated transport failure after retries")
        return super().adjudicate(item, extracted)


def _run_with(
    repository: _MemoryRepository, adjudicator: Adjudicator, mapping: MappingResult
) -> list[str]:
    result = run_document(
        b"%PDF-fake",
        filename="doc.pdf",
        router=_FakeRouter(_stamped_scan_extracted()),
        mapper=_FakeMapper(mapping),
        repository=repository,
        adjudicator=adjudicator,
    )
    return [outcome.disposition for outcome in result.adjudications]


class TestAdjudicatorContainment:
    def _mapping_with_reject_and_flags(self) -> MappingResult:
        """Findings this batch triages to, deterministically:

        - bracket_overlap REJECT on index 2 ([500, 8000] into [0, 9000]) —
          the record is ABSENT from the fact table;
        - bracket_gap FLAGs on indexes 1 and 2 (the hole the reject leaves);
        - confidence_floor FLAG on index 3 (separate chain, lone bracket).

        Four queue entries: one REJECT-born, three FLAG-born.
        """
        return MappingResult(
            records=[
                _record(0, 9000),
                _record(9001, None),
                _record(500, 8000),
                _record(
                    0,
                    9000,
                    filing_status=FilingStatus.MARRIED_FILING_JOINTLY,
                    confidence=Decimal("0.5"),
                ),
            ],
            issues=[],
        )

    def test_reject_class_items_never_auto_resolve(self) -> None:
        repository = _MemoryRepository()
        dispositions = _run_with(
            repository, _ConfidentAdjudicator(), self._mapping_with_reject_and_flags()
        )
        # Every FLAG-born item auto-resolved; the REJECT-born item stayed
        # open with the proposal stored — its open row is the lost record's
        # only trace, whatever the adjudicator's confidence.
        assert sorted(dispositions) == [
            "auto_resolved",
            "auto_resolved",
            "auto_resolved",
            "proposal_stored",
        ]
        overlap_item = next(i for i in repository.items if i.reason.startswith("bracket_overlap"))
        assert repository.status[overlap_item.id] == "open"
        assert overlap_item.id in repository.proposals
        floor_item = next(i for i in repository.items if i.reason.startswith("confidence_floor"))
        assert repository.status[floor_item.id] == "resolved"
        assert repository.resolutions[floor_item.id][1] == "adjudicator:test-model"

    def test_unknown_reason_is_denied_by_default(self) -> None:
        repository = _MemoryRepository()
        handle = repository.register_document(sha256="ab" * 32, filename="x.pdf", byte_size=1)
        repository.queue_review(
            handle.id, [{"reason": "future_writer: something new", "source_page": 1}]
        )
        from tax_tables.pipeline import _adjudicate_queue

        outcomes = _adjudicate_queue(
            repository,
            _ConfidentAdjudicator(),
            handle.id,
            _stamped_scan_extracted(),
            Decimal("0.9"),
        )
        assert [o.disposition for o in outcomes] == ["proposal_stored"]
        assert repository.status[repository.items[0].id] == "open"

    def test_transport_failure_is_contained_per_item(self) -> None:
        repository = _MemoryRepository()
        dispositions = _run_with(
            repository,
            _TransportFailingAdjudicator("confidence_floor"),
            self._mapping_with_reject_and_flags(),
        )
        # The failed item is an error outcome; every other item still got
        # its write; nothing raised past the pass (a raise here would
        # discard a result whose records are already committed).
        assert sorted(dispositions) == [
            "auto_resolved",
            "auto_resolved",
            "error",
            "proposal_stored",
        ]
        floor_item = next(i for i in repository.items if i.reason.startswith("confidence_floor"))
        assert repository.status[floor_item.id] == "open"  # untouched by the failure

    def test_write_race_is_contained_per_item(self) -> None:
        repository = _MemoryRepository()
        repository.fail_resolves = True  # every resolve loses the race
        dispositions = _run_with(
            repository, _ConfidentAdjudicator(), self._mapping_with_reject_and_flags()
        )
        # The three eligible items' resolves race and are reported as
        # errors; the REJECT item's proposal write still happens.
        assert sorted(dispositions) == ["error", "error", "error", "proposal_stored"]

    def test_second_pass_never_re_pays_for_proposed_items(self) -> None:
        """Re-ingesting a document re-adjudicates only items with no stored
        proposal (promoted review minor): the below-threshold proposal from
        pass one awaits its human; the errored item is retried."""
        from tax_tables.pipeline import _adjudicate_queue

        repository = _MemoryRepository()
        handle = repository.register_document(sha256="ab" * 32, filename="x.pdf", byte_size=1)
        repository.queue_review(
            handle.id,
            [
                {"reason": "bracket_overlap: [500, 8000] overlaps", "source_page": 1},
                {"reason": "mapping: unreadable cell", "source_page": 1},
            ],
        )
        first = _TransportFailingAdjudicator("mapping")
        outcomes = _adjudicate_queue(
            repository, first, handle.id, _stamped_scan_extracted(), Decimal("0.9")
        )
        assert sorted(o.disposition for o in outcomes) == ["error", "proposal_stored"]

        class _Counting(_ConfidentAdjudicator):
            def __init__(self) -> None:
                self.seen: list[str] = []

            def adjudicate(self, item: ReviewItem, extracted: ExtractedDocument) -> Adjudication:
                self.seen.append(item.reason)
                return super().adjudicate(item, extracted)

        second = _Counting()
        _adjudicate_queue(repository, second, handle.id, _stamped_scan_extracted(), Decimal("0.9"))
        # Only the errored (never-proposed) item is re-examined; the
        # overlap item's stored proposal is not paid for again.
        assert second.seen == ["mapping: unreadable cell"]

    def test_error_outcome_carries_the_failed_calls_cost(self) -> None:
        """A truncated response was still paid for: the error outcome keeps
        the spend the port error carries (promoted review minor)."""
        from tax_tables.pipeline import _adjudicate_queue
        from tax_tables.ports.adjudicator import AdjudicationError

        spent = MappingCost(engine="test-model", api_calls=1, output_tokens=500)

        class _PaidFailure(_ConfidentAdjudicator):
            def adjudicate(self, item: ReviewItem, extracted: ExtractedDocument) -> Adjudication:
                raise AdjudicationError("stop_reason='max_tokens'", cost=spent)

        repository = _MemoryRepository()
        handle = repository.register_document(sha256="ab" * 32, filename="x.pdf", byte_size=1)
        repository.queue_review(handle.id, [{"reason": "confidence_floor: 0.5", "source_page": 1}])
        (outcome,) = _adjudicate_queue(
            repository, _PaidFailure(), handle.id, _stamped_scan_extracted(), Decimal("0.9")
        )
        assert outcome.disposition == "error"
        assert outcome.error_cost == spent
