"""A verifier that cannot answer must neither lose the document nor bless it.

The baseline gate made this concrete. Document 04 produced the run's only
fully conformant mapper response — 19 records, zero mapping issues — and was
then discarded whole because `alibaba/qwen-3-235b` returned a body with no
verdicts envelope. Nineteen sound records were lost to the second opinion's
own contract failure.

The two obvious repairs are both wrong. Raising loses sound data. Persisting
clean asserts an independent confirmation that never happened, which is
exactly the silent assent ADR 012 forbids. So the records persist flagged,
under their own rule, with the reason queued.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tax_tables.domain.records import (
    CanonicalRecord,
    FilingStatus,
    LifecycleStatus,
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
from tax_tables.pipeline import run_document, unverified_findings
from tax_tables.ports.mapper import MappingResult
from tax_tables.ports.verifier import (
    RecordVerdict,
    Verdict,
    VerificationError,
    VerificationResult,
)
from tax_tables.validation.validators import RULE_VERIFIER_UNAVAILABLE, Severity


def _document() -> ExtractedDocument:
    table = ExtractedTable(
        page_number=1,
        table_id="p1_t0",
        bbox=(0.0, 0.0, 100.0, 50.0),
        grid_source=GridSource.RULED_LINES,
        rows=[[Cell(text="Rate"), Cell(text="Single")], [Cell(text="10%"), Cell(text="$0")]],
        column_count=2,
    )
    return ExtractedDocument(
        filename="04_employment_tax_rates_and_thresholds_2026.pdf",
        sha256="cd" * 32,
        pages=[
            PageExtraction(
                page_number=1,
                width=612.0,
                height=792.0,
                method=ExtractionMethod.DETERMINISTIC_TEXT,
                tables=[table],
                prose=[],
            )
        ],
        cost=ExtractionCost(engine="pdfplumber", wall_seconds=0.1),
    )


def _record(lower: int = 0, upper: int | None = None) -> CanonicalRecord:
    return CanonicalRecord(
        source_page=1,
        table_id="p1_t0",
        record_type=RecordType.ORDINARY_INCOME_BRACKET,
        jurisdiction="US",
        attribute_key=None,
        filing_status=FilingStatus.SINGLE,
        taxpayer_class=None,
        tax_year=2026,
        lifecycle_status=LifecycleStatus.ACTIVE,
        lower_bound=lower,
        upper_bound=upper,
        rate=Decimal("0.10"),
        amount=None,
        currency="USD",
        attrs={"source_table_label": "table_1"},
        confidence=Decimal("0.97"),
        review_status=ReviewStatus.CLEAN,
    )


class _Router:
    def extract(self, pdf_bytes: bytes, *, filename: str) -> ExtractedDocument:
        return _document()


class _Mapper:
    def __init__(self, records: list[CanonicalRecord]) -> None:
        self._records = records

    def map_document(self, extracted: ExtractedDocument) -> MappingResult:
        return MappingResult(records=list(self._records), issues=[])


class _BrokenVerifier:
    def verify(self, extracted: ExtractedDocument, mapping: MappingResult) -> VerificationResult:
        raise VerificationError("verification response JSON lacks the verdicts envelope")


class _AgreeingVerifier:
    def verify(self, extracted: ExtractedDocument, mapping: MappingResult) -> VerificationResult:
        return VerificationResult(
            verdicts=[
                RecordVerdict(record_index=i, verdict=Verdict.CONFIRMED, reason=None)
                for i in range(len(mapping.records))
            ]
        )


def _run(verifier: Any, records: list[CanonicalRecord]) -> Any:
    return run_document(
        b"%PDF-1.4",
        filename="04_employment_tax_rates_and_thresholds_2026.pdf",
        router=_Router(),
        mapper=_Mapper(records),
        verifier=verifier,
    )


class TestContainment:
    def test_the_records_are_not_lost(self) -> None:
        records = [_record(0, 99), _record(100, 199), _record(200)]
        result = _run(_BrokenVerifier(), records)
        assert len(result.mapping.records) == 3
        assert len(result.triage.persistable) == 3

    def test_every_record_is_flagged_never_clean(self) -> None:
        result = _run(_BrokenVerifier(), [_record(0, 99), _record(100)])
        assert all(r.review_status is ReviewStatus.NEEDS_REVIEW for r in result.triage.persistable)

    def test_the_reason_reaches_the_review_queue(self) -> None:
        result = _run(_BrokenVerifier(), [_record(0)])
        findings = [f for f in result.triage.findings if f.rule == RULE_VERIFIER_UNAVAILABLE]
        assert len(findings) == 1
        assert "verdicts envelope" in findings[0].detail
        assert findings[0].severity is Severity.FLAG

    def test_the_verification_result_says_unavailable_not_agreed(self) -> None:
        """`disputes 0` must never be readable as a clean bill of health when
        the verifier was never able to speak."""
        result = _run(_BrokenVerifier(), [_record(0)])
        verification = result.verification
        assert verification is not None
        assert verification.verdicts == []
        assert verification.notes and "verifier unavailable" in verification.notes[0]

    def test_a_working_verifier_is_unaffected(self) -> None:
        result = _run(_AgreeingVerifier(), [_record(0, 99), _record(100)])
        assert all(r.review_status is ReviewStatus.CLEAN for r in result.triage.persistable)
        assert not [f for f in result.triage.findings if f.rule == RULE_VERIFIER_UNAVAILABLE]


class TestFindings:
    def test_one_finding_per_record_in_order(self) -> None:
        findings = unverified_findings([object(), object(), object()], "verifier unavailable: x")
        assert [f.record_index for f in findings] == [0, 1, 2]
        assert all(f.severity is Severity.FLAG for f in findings)

    def test_no_records_means_no_findings(self) -> None:
        assert unverified_findings([], "verifier unavailable: x") == []
