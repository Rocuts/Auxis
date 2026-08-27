"""Pipeline composition tests: grid -> mapper -> validators -> persist.

Router and mapper are faked (their own suites cover them); triage and the
Postgres repository are real — these tests run against the docker-compose
database and pin the persistence side of anti-goal #8: everything the
mapper produced is accounted for as a persisted row or a review-queue
entry, never dropped.
"""

from __future__ import annotations

import json
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


def _natural_key(record: CanonicalRecord) -> tuple[Any, ...]:
    """The ``records_natural_key`` columns, in the constraint's order."""
    return (
        record.jurisdiction,
        str(record.record_type),
        record.attribute_key,
        record.tax_year,
        None if record.filing_status is None else str(record.filing_status),
        record.taxpayer_class,
        str(record.lifecycle_status),
        record.lower_bound,
        record.upper_bound,
    )


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
        #: The fact table, keyed like ``records_natural_key``. ``ingest``
        #: fills it, so ``record_present`` answers from what was actually
        #: accepted rather than from what was offered.
        self.stored: set[tuple[Any, ...]] = set()
        #: Natural keys ``ingest`` must refuse, standing in for the
        #: exclusion constraint and the cross-document natural key.
        self.refuse: set[tuple[Any, ...]] = set()

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
        # The port contract is REPLACE, not merge: this document's previous
        # set goes before the new one lands. The fake honours it so it cannot
        # quietly diverge from the real adapter (this fake holds one
        # document, so clearing `stored` is the faithful analogue).
        self.stored.clear()
        persisted = 0
        refused = 0
        for record in records:
            key = _natural_key(record)
            if key in self.refuse:
                refused += 1
                continue
            self.stored.add(key)
            persisted += 1
        return IngestOutcome(
            persisted=persisted, cross_document_conflicts=0, overlap_rejections=refused
        )

    def record_present(self, document_id: UUID, record: CanonicalRecord) -> bool:
        return _natural_key(record) in self.stored

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
        # TWO items stay open, not one, and the second is the whole point:
        # index 2 collects the bracket_overlap REJECT *and* a bracket_gap
        # FLAG, so it queues one row of each while the record itself reaches
        # no table. Eligibility keyed on the reason prefix auto-closed that
        # FLAG row — this assertion used to encode exactly that defect.
        # Keyed on presence, both of index 2's rows stay with a human.
        assert sorted(dispositions) == [
            "auto_resolved",
            "auto_resolved",
            "proposal_stored",
            "proposal_stored",
        ]
        overlap_item = next(i for i in repository.items if i.reason.startswith("bracket_overlap"))
        assert repository.status[overlap_item.id] == "open"
        assert overlap_item.id in repository.proposals
        floor_item = next(i for i in repository.items if i.reason.startswith("confidence_floor"))
        assert repository.status[floor_item.id] == "resolved"
        assert repository.resolutions[floor_item.id][1] == "adjudicator:test-model"

    def test_confidence_floor_row_of_a_rejected_record_never_auto_resolves(self) -> None:
        """The exact reachable path the reason-prefix gate could not see.

        One record collects BOTH findings: ``confidence_floor`` (a FLAG, and
        an auto-resolvable rule) and ``bracket_overlap`` (a REJECT). Triage
        refuses the record, so nothing reaches the fact table — yet it
        queues one row under each finding, and the confidence_floor row's
        rule name is on the eligible list. Keyed on the rule name, that row
        auto-closed a record the database never held; keyed on presence, it
        cannot.

        This is document 05's shape. There the FLAG happened to be
        ``verifier_unavailable``, which gate 1 default-denies for unrelated
        reasons — the adjudicator had already endorsed the absent record at
        0.95 with valid, mechanically supported citations, so only the rule
        name stood between it and an unattended close (ADR 014 §8a).
        """
        repository = _MemoryRepository()
        mapping = MappingResult(
            records=[
                _record(0, 9000),
                _record(500, 8000, confidence=Decimal("0.5")),
            ],
            issues=[],
        )
        dispositions = _run_with(repository, _ConfidentAdjudicator(), mapping)

        floor_item = next(i for i in repository.items if i.reason.startswith("confidence_floor"))
        overlap_item = next(i for i in repository.items if i.reason.startswith("bracket_overlap"))
        # Both rows stand for the SAME record, and that record is absent.
        # (If the two findings ever landed on different records this test
        # would silently stop testing the path it names.)
        for item in (floor_item, overlap_item):
            assert item.raw_value is not None
            assert json.loads(item.raw_value)["lower_bound"] == 500
        assert repository.record_present(repository.document_id, _record(500, 8000)) is False
        assert repository.status[floor_item.id] == "open"
        assert repository.status[overlap_item.id] == "open"
        # The adjudicator still did its work on both — the proposal is
        # stored for the human, which is the point: nothing is dropped,
        # nothing is closed (anti-goal #8).
        assert floor_item.id in repository.proposals
        assert overlap_item.id in repository.proposals
        assert "auto_resolved" not in dispositions

    def test_flag_only_record_refused_at_ingest_never_auto_resolves(self) -> None:
        """The second reachable path: triage said yes, the DATABASE said no.

        ``triage.persistable`` is a proposal, not an outcome. A FLAG-only
        record still meets the exclusion constraint and the natural key at
        ingest and can be refused there — which is what happened to document
        05's four "Over $X" brackets. Its FLAG row then describes a record
        the fact table does not hold, and no inspection of the row's reason
        could ever reveal that.
        """
        repository = _MemoryRepository()
        record = _record(0, 9000, confidence=Decimal("0.5"))
        repository.refuse.add(_natural_key(record))  # the constraint refuses it
        dispositions = _run_with(
            repository, _ConfidentAdjudicator(), MappingResult(records=[record], issues=[])
        )

        (floor_item,) = repository.items
        assert floor_item.reason.startswith("confidence_floor")
        assert repository.record_present(repository.document_id, record) is False
        assert dispositions == ["proposal_stored"]
        assert repository.status[floor_item.id] == "open"

    def test_present_record_still_auto_resolves(self) -> None:
        """The gate must not be a blanket refusal: the SAME finding on a
        record the fact table accepted still closes. A guard that refuses
        correct work is not conservative, it is broken."""
        repository = _MemoryRepository()
        record = _record(0, 9000, confidence=Decimal("0.5"))
        dispositions = _run_with(
            repository, _ConfidentAdjudicator(), MappingResult(records=[record], issues=[])
        )
        (floor_item,) = repository.items
        assert repository.record_present(repository.document_id, record) is True
        assert dispositions == ["auto_resolved"]
        assert repository.status[floor_item.id] == "resolved"

    def test_mapping_issue_row_carries_no_record_and_is_denied(self) -> None:
        """A ``"mapping: ..."`` row's raw_value is the offending CELL, not a
        record. There is nothing to look up, so there is nothing to close —
        default-deny rather than an exception."""
        from tax_tables.pipeline import adjudicate_open_items

        repository = _MemoryRepository()
        handle = repository.register_document(sha256="cd" * 32, filename="x.pdf", byte_size=1)
        repository.queue_review(
            handle.id,
            [{"reason": "mapping: unreadable cell", "source_page": 1, "raw_value": "12,3-4"}],
        )
        outcomes = adjudicate_open_items(
            repository,
            _ConfidentAdjudicator(),
            handle.id,
            _stamped_scan_extracted(),
            Decimal("0.9"),
        )
        assert [o.disposition for o in outcomes] == ["proposal_stored"]
        assert repository.status[repository.items[0].id] == "open"

    def test_unknown_reason_is_denied_by_default(self) -> None:
        repository = _MemoryRepository()
        handle = repository.register_document(sha256="ab" * 32, filename="x.pdf", byte_size=1)
        repository.queue_review(
            handle.id, [{"reason": "future_writer: something new", "source_page": 1}]
        )
        from tax_tables.pipeline import adjudicate_open_items

        outcomes = adjudicate_open_items(
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
            "error",
            "proposal_stored",
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
        # Only the two PRESENT records' items reach a resolve and race;
        # both of index 2's rows (absent record) take the proposal path,
        # which does not fail.
        assert sorted(dispositions) == ["error", "error", "proposal_stored", "proposal_stored"]

    def test_second_pass_never_re_pays_for_proposed_items(self) -> None:
        """Re-ingesting a document re-adjudicates only items with no stored
        proposal (promoted review minor): the below-threshold proposal from
        pass one awaits its human; the errored item is retried."""
        from tax_tables.pipeline import adjudicate_open_items

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
        outcomes = adjudicate_open_items(
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
        adjudicate_open_items(
            repository, second, handle.id, _stamped_scan_extracted(), Decimal("0.9")
        )
        # Only the errored (never-proposed) item is re-examined; the
        # overlap item's stored proposal is not paid for again.
        assert second.seen == ["mapping: unreadable cell"]

    def test_error_outcome_carries_the_failed_calls_cost(self) -> None:
        """A truncated response was still paid for: the error outcome keeps
        the spend the port error carries (promoted review minor)."""
        from tax_tables.pipeline import adjudicate_open_items
        from tax_tables.ports.adjudicator import AdjudicationError

        spent = MappingCost(engine="test-model", api_calls=1, output_tokens=500)

        class _PaidFailure(_ConfidentAdjudicator):
            def adjudicate(self, item: ReviewItem, extracted: ExtractedDocument) -> Adjudication:
                raise AdjudicationError("stop_reason='max_tokens'", cost=spent)

        repository = _MemoryRepository()
        handle = repository.register_document(sha256="ab" * 32, filename="x.pdf", byte_size=1)
        repository.queue_review(handle.id, [{"reason": "confidence_floor: 0.5", "source_page": 1}])
        (outcome,) = adjudicate_open_items(
            repository, _PaidFailure(), handle.id, _stamped_scan_extracted(), Decimal("0.9")
        )
        assert outcome.disposition == "error"
        assert outcome.error_cost == spent


class TestAdjudicationBudget:
    """The adjudication pass is bounded in wall clock.

    Production, 2026-08-27: document 01 persisted its records at ~360 s and
    then spent the rest of a 1800 s invocation in adjudication without
    terminating — twice. The pass is optional by design (an item it cannot
    settle waits for a human), so an unbounded one trades a *finished job*
    for a *resolved queue item*, which is the wrong way round.
    """

    @staticmethod
    def _repo_with(n_items: int) -> tuple[_MemoryRepository, UUID]:
        repository = _MemoryRepository()
        handle = repository.register_document(sha256="ef" * 32, filename="x.pdf", byte_size=1)
        repository.queue_review(
            handle.id,
            [
                {"reason": f"mapping: cell {i}", "source_page": 1, "raw_value": "?"}
                for i in range(n_items)
            ],
        )
        return repository, handle.id

    def test_exhausted_budget_stops_the_pass(self) -> None:
        from tax_tables.pipeline import adjudicate_open_items

        repository, document_id = self._repo_with(3)
        outcomes = adjudicate_open_items(
            repository,
            _ConfidentAdjudicator(),
            document_id,
            _stamped_scan_extracted(),
            Decimal("0.9"),
            budget_seconds=0,  # already exhausted on entry
        )
        assert [o.disposition for o in outcomes] == ["deadline_exceeded"] * 3

    def test_skipped_items_are_reported_not_dropped(self) -> None:
        """Anti-goal #8: an item the pass did not reach must still appear in
        the report, with a reason. A queue item that silently vanished from
        the run's output is invisible loss."""
        from tax_tables.pipeline import adjudicate_open_items

        repository, document_id = self._repo_with(2)
        outcomes = adjudicate_open_items(
            repository,
            _ConfidentAdjudicator(),
            document_id,
            _stamped_scan_extracted(),
            Decimal("0.9"),
            budget_seconds=0,
        )
        assert len(outcomes) == 2
        for outcome in outcomes:
            assert outcome.error is not None
            assert "budget" in outcome.error
        # Untouched: every item is still open and waiting for its human.
        assert all(status == "open" for status in repository.status.values())

    def test_no_budget_means_every_item_is_adjudicated(self) -> None:
        """The budget is opt-in; omitting it preserves the previous
        behaviour exactly, so local and harness runs are unaffected."""
        from tax_tables.pipeline import adjudicate_open_items

        repository, document_id = self._repo_with(2)
        outcomes = adjudicate_open_items(
            repository,
            _ConfidentAdjudicator(),
            document_id,
            _stamped_scan_extracted(),
            Decimal("0.9"),
        )
        assert [o.disposition for o in outcomes] == ["proposal_stored"] * 2
