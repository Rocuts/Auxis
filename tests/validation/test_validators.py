"""Validator + triage invariants, on hand-built records only.

Deliberately synthetic: these rules are about relationships *between*
records, so fixture-derived data would only make the arrangements harder to
read. The values chosen (12250/12251 boundaries, the -0.0003 rebate, the
state/local/combined triple) mirror shapes the corpus actually contains, so
the rules stay calibrated to real documents.

The load-bearing invariant, asserted repeatedly: triage partitions its input.
Nothing is dropped, nothing is guessed (anti-goal #8).
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
from tax_tables.validation.validators import (
    RULE_BRACKET_BOTTOM,
    RULE_BRACKET_GAP,
    RULE_BRACKET_OVERLAP,
    RULE_CONFIDENCE_FLOOR,
    RULE_DERIVED_SUM,
    RULE_OPEN_TOP,
    RULE_RATE_PLAUSIBILITY,
    Finding,
    Severity,
    TriageResult,
    review_queue_entry,
    triage,
    validate_batch,
)


def bracket(
    lower: int,
    upper: int | None,
    *,
    rate: str | None = "0.10",
    filing_status: FilingStatus = FilingStatus.SINGLE,
    tax_year: int = 2026,
    taxpayer_class: str | None = None,
    lifecycle_status: LifecycleStatus = LifecycleStatus.ACTIVE,
    confidence: str = "0.95",
) -> CanonicalRecord:
    """A bracket record with the chain the domain model requires
    (tax_year + filing_status are mandatory for brackets)."""
    return CanonicalRecord(
        source_page=1,
        table_id="p1_t0",
        record_type=RecordType.ORDINARY_INCOME_BRACKET,
        jurisdiction="US",
        filing_status=filing_status,
        taxpayer_class=taxpayer_class,
        tax_year=tax_year,
        lifecycle_status=lifecycle_status,
        lower_bound=lower,
        upper_bound=upper,
        rate=None if rate is None else Decimal(rate),
        currency="USD",
        confidence=Decimal(confidence),
    )


def scalar(
    *,
    record_type: RecordType = RecordType.SALES_TAX_RATE,
    jurisdiction: str = "US-CA",
    rate: str | None = None,
    attrs: dict[str, Any] | None = None,
    confidence: str = "0.95",
) -> CanonicalRecord:
    """A non-bracket record: no bounds, so the bracket rules ignore it."""
    return CanonicalRecord(
        source_page=1,
        table_id="p1_t0",
        record_type=record_type,
        jurisdiction=jurisdiction,
        rate=None if rate is None else Decimal(rate),
        attrs=attrs or {},
        confidence=Decimal(confidence),
    )


def rules_for(findings: list[Finding], index: int) -> set[str]:
    return {f.rule for f in findings if f.record_index == index}


def assert_partitions(result: TriageResult, records: list[CanonicalRecord]) -> None:
    """Nothing dropped: every input ends up in exactly one output list."""
    assert len(result.persistable) + len(result.rejected) == len(records)
    assert result.record_count == len(records)


class TestBracketOverlap:
    def test_second_overlapping_bracket_is_rejected_first_persists(self) -> None:
        records = [bracket(0, 12250), bracket(10000, 20000)]
        result = triage(records)
        assert [r.index for r in result.rejected] == [1]
        assert RULE_BRACKET_OVERLAP in {f.rule for f in result.rejected[0].findings}
        assert result.rejected[0].findings[0].severity is Severity.REJECT
        assert records[0] in result.persistable
        assert_partitions(result, records)

    def test_adjacent_brackets_do_not_overlap(self) -> None:
        records = [bracket(0, 12250), bracket(12251, 49800)]
        assert RULE_BRACKET_OVERLAP not in {f.rule for f in validate_batch(records)}

    def test_open_top_overlaps_anything_above_its_lower_bound(self) -> None:
        records = [bracket(49801, None), bracket(60000, 70000)]
        findings = validate_batch(records)
        assert RULE_BRACKET_OVERLAP in rules_for(findings, 1)

    def test_touching_bounds_overlap(self) -> None:
        # Inclusive bounds: 12250 belongs to both brackets.
        records = [bracket(0, 12250), bracket(12250, 49800)]
        assert RULE_BRACKET_OVERLAP in rules_for(validate_batch(records), 1)

    def test_separate_chains_do_not_interact(self) -> None:
        records = [
            bracket(0, 12250, filing_status=FilingStatus.SINGLE),
            bracket(0, 12250, filing_status=FilingStatus.MARRIED_FILING_JOINTLY),
            bracket(0, 12250, tax_year=2025),
            bracket(0, 12250, lifecycle_status=LifecycleStatus.SUPERSEDED),
        ]
        assert RULE_BRACKET_OVERLAP not in {f.rule for f in validate_batch(records)}
        assert triage(records).rejected == []

    def test_none_and_empty_taxpayer_class_share_a_chain(self) -> None:
        # Mirrors the DDL's COALESCE(taxpayer_class, ''): a NULL discriminator
        # must not let a bracket slip past the overlap check.
        records = [bracket(0, 12250, taxpayer_class=None), bracket(0, 12250, taxpayer_class="")]
        assert RULE_BRACKET_OVERLAP in rules_for(validate_batch(records), 1)


class TestBracketGap:
    def test_hole_flags_both_neighbours_and_both_persist(self) -> None:
        records = [bracket(0, 12250), bracket(12300, 49800)]
        result = triage(records)
        assert RULE_BRACKET_GAP in rules_for(result.findings, 0)
        assert RULE_BRACKET_GAP in rules_for(result.findings, 1)
        gap = next(f for f in result.findings if f.rule == RULE_BRACKET_GAP)
        assert gap.severity is Severity.FLAG
        assert "[12251, 12299]" in gap.detail
        assert result.rejected == []
        assert [r.review_status for r in result.persistable] == [
            ReviewStatus.NEEDS_REVIEW,
            ReviewStatus.NEEDS_REVIEW,
        ]
        assert_partitions(result, records)

    def test_adjacent_brackets_have_no_gap(self) -> None:
        records = [bracket(0, 12250), bracket(12251, 49800), bracket(49801, None)]
        assert RULE_BRACKET_GAP not in {f.rule for f in validate_batch(records)}

    def test_gap_detected_regardless_of_batch_order(self) -> None:
        records = [bracket(12300, 49800), bracket(0, 12250)]
        assert RULE_BRACKET_GAP in {f.rule for f in validate_batch(records)}

    def test_overlap_is_not_also_reported_as_a_gap(self) -> None:
        records = [bracket(0, 12250), bracket(10000, 20000)]
        assert RULE_BRACKET_GAP not in {f.rule for f in validate_batch(records)}


class TestBracketBottom:
    def test_chain_starting_at_zero_is_clean(self) -> None:
        records = [bracket(0, 11925), bracket(11926, None)]
        assert RULE_BRACKET_BOTTOM not in {f.rule for f in validate_batch(records)}

    def test_chain_with_uncovered_head_is_flagged_on_its_lowest_bracket(self) -> None:
        # The classic symptom of a first data row lost to a header band:
        # pairwise gap checks cannot see it, the missing row is its own only
        # evidence. The chain's lowest surviving bracket carries the flag.
        records = [bracket(11926, 48475), bracket(48476, None)]
        result = triage(records)
        assert rules_for(result.findings, 0) == {RULE_BRACKET_BOTTOM}
        finding = next(f for f in result.findings if f.rule == RULE_BRACKET_BOTTOM)
        assert "[0, 11925]" in finding.detail
        assert result.persistable[0].review_status is ReviewStatus.NEEDS_REVIEW
        assert_partitions(result, records)

    def test_lone_threshold_bracket_may_start_high(self) -> None:
        records = [bracket(1_000_000, None)]
        assert RULE_BRACKET_BOTTOM not in {f.rule for f in validate_batch(records)}


class TestOpenTop:
    def test_open_ended_top_is_clean(self) -> None:
        records = [bracket(0, 12250), bracket(12251, 49800), bracket(49801, None)]
        result = triage(records)
        assert result.findings == []
        assert result.persistable == records
        assert result.rejected == []

    def test_two_open_ended_brackets_are_flagged(self) -> None:
        records = [bracket(0, None), bracket(12251, None)]
        findings = validate_batch(records)
        assert RULE_OPEN_TOP in rules_for(findings, 0)
        assert RULE_OPEN_TOP in rules_for(findings, 1)

    def test_bounded_top_is_flagged_on_the_highest_bracket(self) -> None:
        records = [bracket(0, 12250), bracket(12251, 49800)]
        findings = [f for f in validate_batch(records) if f.rule == RULE_OPEN_TOP]
        assert [f.record_index for f in findings] == [1]
        assert findings[0].severity is Severity.FLAG

    def test_single_bracket_chain_is_not_judged(self) -> None:
        # One bracket says nothing about where the schedule terminates.
        assert validate_batch([bracket(0, 12250)]) == []

    def test_open_bracket_below_a_bounded_one_is_flagged(self) -> None:
        # Reachable only across chains-of-one plus a deliberate mis-read; the
        # rule states the invariant independently of how it got violated.
        records = [bracket(0, None), bracket(12251, 49800)]
        findings = [f for f in validate_batch(records) if f.rule == RULE_OPEN_TOP]
        assert [f.record_index for f in findings] == [0]


class TestRatePlausibility:
    def test_statutory_rebate_passes_clean(self) -> None:
        # Document 03's legitimate negative local rate, as a fraction.
        record = scalar(rate="-0.0003")
        assert validate_batch([record]) == []

    def test_unconverted_percentage_is_flagged(self) -> None:
        findings = validate_batch([scalar(rate="0.7")])
        assert [f.rule for f in findings] == [RULE_RATE_PLAUSIBILITY]
        assert findings[0].severity is Severity.FLAG

    def test_missing_rate_is_not_a_finding(self) -> None:
        # A null rate means "no tax imposed" — a fact, not an omission.
        assert validate_batch([scalar(rate=None)]) == []

    def test_implausible_negative_is_flagged(self) -> None:
        assert RULE_RATE_PLAUSIBILITY in rules_for(validate_batch([scalar(rate="-0.2")]), 0)


class TestConfidenceFloor:
    def test_low_confidence_flags_and_copy_needs_review(self) -> None:
        record = scalar(rate="0.0725", confidence="0.4")
        result = triage([record])
        assert [f.rule for f in result.findings] == [RULE_CONFIDENCE_FLOOR]
        assert result.persistable[0].review_status is ReviewStatus.NEEDS_REVIEW
        # The input is frozen and must never be mutated: the harness compares
        # against the mapper's raw output.
        assert record.review_status is ReviewStatus.CLEAN
        assert result.persistable[0] is not record

    def test_floor_is_configurable(self) -> None:
        record = scalar(confidence="0.8")
        assert validate_batch([record]) == []
        assert rules_for(validate_batch([record], confidence_floor=Decimal("0.9")), 0) == {
            RULE_CONFIDENCE_FLOOR
        }

    def test_confidence_at_the_floor_passes(self) -> None:
        assert validate_batch([scalar(confidence="0.7")]) == []


class TestDerivedSum:
    def test_consistent_triple_passes(self) -> None:
        record = scalar(
            attrs={
                "state_rate_pct": "6.25",
                "avg_local_rate_pct": "1.43",
                "combined_rate_pct": "7.68",
            }
        )
        assert validate_batch([record]) == []

    def test_partial_triple_is_flagged_not_skipped(self) -> None:
        # A mapper that lost one of the three columns is exactly the case
        # this rule exists for; skipping it would disable the only
        # cross-check at the moment it matters.
        record = scalar(
            attrs={
                "state_rate_pct": "6.25",
                "avg_local_rate_pct": "1.43",
            }
        )
        findings = validate_batch([record])
        assert rules_for(findings, 0) == {RULE_DERIVED_SUM}
        assert "combined_rate_pct" in findings[0].detail

    def test_inconsistent_triple_is_flagged(self) -> None:
        record = scalar(
            attrs={
                "state_rate_pct": "6.25",
                "avg_local_rate_pct": "1.43",
                "combined_rate_pct": "8.68",
            }
        )
        assert rules_for(validate_batch([record]), 0) == {RULE_DERIVED_SUM}

    def test_null_local_counts_as_zero_without_being_flagged(self) -> None:
        # No local tax imposed: the null stays null in attrs and contributes
        # 0 to the identity. It is not a missing value.
        record = scalar(
            attrs={
                "state_rate_pct": "4.0",
                "avg_local_rate_pct": None,
                "combined_rate_pct": "4.0",
            }
        )
        assert validate_batch([record]) == []
        assert record.attrs["avg_local_rate_pct"] is None

    def test_all_null_triple_is_skipped(self) -> None:
        record = scalar(
            attrs={
                "state_rate_pct": None,
                "avg_local_rate_pct": None,
                "combined_rate_pct": None,
            }
        )
        assert validate_batch([record]) == []

    def test_numeric_json_types_are_accepted(self) -> None:
        record = scalar(
            attrs={
                "state_rate_pct": 6.25,
                "avg_local_rate_pct": 1.43,
                "combined_rate_pct": 7.68,
            }
        )
        assert validate_batch([record]) == []

    def test_unparseable_attribute_is_flagged_not_raised(self) -> None:
        record = scalar(
            attrs={
                "state_rate_pct": "6.25%",
                "avg_local_rate_pct": "1.43",
                "combined_rate_pct": "7.68",
            }
        )
        assert rules_for(validate_batch([record]), 0) == {RULE_DERIVED_SUM}


class TestTriageConservation:
    def test_mixed_batch_partitions_and_keeps_every_record(self) -> None:
        records = [
            bracket(0, 12250),
            bracket(10000, 20000),  # overlaps -> rejected
            bracket(30000, 49800, confidence="0.3"),  # low confidence -> flagged
            bracket(49801, None),
            scalar(rate="0.9"),  # implausible rate -> flagged
        ]
        result = triage(records)
        assert_partitions(result, records)
        assert [r.index for r in result.rejected] == [1]
        assert result.rejected[0].record is records[1]
        assert all(f.record_index == 1 for f in result.rejected[0].findings)
        assert len(result.persistable) == 4

    def test_clean_batch_returns_originals_untouched(self) -> None:
        records = [bracket(0, 12250), bracket(12251, None)]
        result = triage(records)
        assert result.persistable == records
        assert all(r.review_status is ReviewStatus.CLEAN for r in result.persistable)

    def test_empty_batch(self) -> None:
        result = triage([])
        assert result == TriageResult(persistable=[], rejected=[], findings=[])

    def test_every_finding_indexes_a_real_record(self) -> None:
        records = [bracket(0, 12250), bracket(10000, 20000), scalar(rate="0.8")]
        result = triage(records)
        assert result.findings
        assert all(0 <= f.record_index < len(records) for f in result.findings)


class TestReviewQueueEntry:
    def test_entry_matches_migration_0004_columns(self) -> None:
        records = [bracket(0, 12250), bracket(10000, 20000)]
        rejected = triage(records).rejected[0]
        entry = review_queue_entry(rejected.record, rejected.findings[0])
        assert set(entry) == {
            "source_page",
            "table_id",
            "row_index",
            "col_index",
            "raw_value",
            "reason",
        }
        assert entry["source_page"] == 1
        assert entry["table_id"] == "p1_t0"
        assert entry["row_index"] is None and entry["col_index"] is None
        assert entry["reason"].startswith(f"{RULE_BRACKET_OVERLAP}: ")
        # raw_value carries the proposed record verbatim, as JSON.
        assert '"lower_bound":10000' in entry["raw_value"]
