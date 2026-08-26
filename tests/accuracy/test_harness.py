"""Tests for the accuracy harness itself, plus structural checks on the oracle.

Two rules shape this file:

- **The machinery is tested without the oracle.** Every ``compare`` /
  ``format_report`` case below is hand-built from synthetic values that
  appear nowhere in ``ground_truth.json``. If the harness could only be
  exercised through the oracle, a bug in the harness and a bug in the
  extraction would be indistinguishable.
- **The oracle is only inspected structurally.** Counts, keys, and the
  existence of the fixture PDFs it names. No extraction and no mapping runs
  here: the SchemaMapper adapter does not exist in Phase 2a, and an accuracy
  table assembled from stubs would be a fabricated gate result (anti-goal #2).
"""

from __future__ import annotations

import ast
import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tax_tables.domain.records import (
    CanonicalRecord,
    FilingStatus,
    LifecycleStatus,
    RecordType,
)
from tests.accuracy.harness import (
    ABSENT,
    FIXTURES_DIR,
    REPO_ROOT,
    ActualRecord,
    ExpectedRecord,
    GroundTruth,
    compare,
    format_report,
    iter_source_documents,
    load_ground_truth,
    natural_key,
)

SRC_DIR = REPO_ROOT / "src"


@pytest.fixture(scope="module")
def truth() -> GroundTruth:
    return load_ground_truth()


# --------------------------------------------------------------------------
# Synthetic builders. Values here are deliberately unlike anything in the
# oracle (jurisdiction "ZZ-TEST", tax year 1999) so a copy-paste from the
# ground truth into the harness could never make these pass.
# --------------------------------------------------------------------------

SYNTHETIC_DOC = "99_synthetic.pdf"


def expected(**overrides: Any) -> ExpectedRecord:
    base: dict[str, Any] = {
        "source_document": SYNTHETIC_DOC,
        "source_page": 1,
        "table_id": "table_z",
        "record_type": RecordType.ORDINARY_INCOME_BRACKET,
        "jurisdiction": "ZZ-TEST",
        "tax_year": 1999,
        "taxpayer_class": "synthetic",
        "filing_status": "single",
        "lower_bound": 0,
        "upper_bound": 700,
        "rate": Decimal("0.11"),
        "currency": "USD",
    }
    base.update(overrides)
    return ExpectedRecord.model_validate(base)


def actual(**overrides: Any) -> ActualRecord:
    base: dict[str, Any] = {
        "source_page": 1,
        # Two key spaces: table_id carries extraction provenance, while the
        # document's own printed label rides in attrs (COMPARED_AS).
        "table_id": "p1_t0",
        "attrs": {"source_table_label": "table_z"},
        "record_type": RecordType.ORDINARY_INCOME_BRACKET,
        "jurisdiction": "ZZ-TEST",
        "tax_year": 1999,
        "taxpayer_class": "synthetic",
        "filing_status": FilingStatus.SINGLE,
        "lower_bound": 0,
        "upper_bound": 700,
        "rate": Decimal("0.11"),
        "currency": "USD",
        "confidence": Decimal("1"),
    }
    document = str(overrides.pop("source_document", SYNTHETIC_DOC))
    attrs_override: dict[str, Any] = overrides.pop("attrs", {})
    base.update(overrides)
    base["attrs"] = {"source_table_label": "table_z", **attrs_override}
    return document, CanonicalRecord.model_validate(base)


class TestGroundTruthStructure:
    """The oracle must be internally consistent before it can judge anything."""

    def test_record_count(self, truth: GroundTruth) -> None:
        assert truth.record_count == 128
        assert truth.totals.records == 128

    def test_per_document_counts_sum_to_total(self, truth: GroundTruth) -> None:
        assert sum(d.expected_record_count for d in truth.documents) == truth.record_count

    def test_per_document_counts_match_the_records(self, truth: GroundTruth) -> None:
        # load_ground_truth already validates this; assert it explicitly so a
        # regression names the failing document rather than a load error.
        for document in truth.documents:
            listed = [r for r in truth.expected_records if r.source_document == document.file]
            assert len(listed) == document.expected_record_count, document.file

    def test_every_source_document_names_an_existing_fixture(self, truth: GroundTruth) -> None:
        declared = {d.file for d in truth.documents}
        for record in truth.expected_records:
            assert record.source_document in declared
            assert (FIXTURES_DIR / record.source_document).is_file()
        assert all(path.is_file() for path in iter_source_documents(truth))

    def test_document_hashes_match_the_pdfs_on_disk(self, truth: GroundTruth) -> None:
        """The sha256 is the document's natural key for idempotency; if a
        fixture is regenerated the oracle must be regenerated with it."""
        for document in truth.documents:
            digest = hashlib.sha256((FIXTURES_DIR / document.file).read_bytes()).hexdigest()
            assert digest == document.sha256, document.file

    def test_natural_keys_are_unique(self, truth: GroundTruth) -> None:
        keys = [natural_key(r) for r in truth.expected_records]
        assert len(set(keys)) == len(keys) == 128

    # This test began life as a strict xfail recording a real domain gap: the
    # oracle uses filing_status='qualifying_surviving_spouse' (02 and 04), a
    # fifth status the FilingStatus enum and the 0003 CHECK did not admit.
    # Migration 0005 + the enum member closed the gap; the test now guards it.
    def test_every_filing_status_is_representable_in_the_domain(self, truth: GroundTruth) -> None:
        known = {status.value for status in FilingStatus}
        unrepresentable = sorted(
            {
                r.filing_status
                for r in truth.expected_records
                if r.filing_status is not None and r.filing_status not in known
            }
        )
        assert unrepresentable == []

    def test_every_oracle_record_is_constructible_as_a_canonical_record(
        self, truth: GroundTruth
    ) -> None:
        """The guard that would have caught both schema gaps at once.

        Enum membership alone missed the estates/trusts brackets (filing
        status absent, taxpayer_class-discriminated), which the domain
        model's shape validator rejected until migration 0006. So: map every
        oracle entry's typed core onto CanonicalRecord, everything else into
        attrs, and name each entry that cannot be built. If this fails, the
        Phase 2 accuracy ceiling is below 128 before any mapper runs.
        """
        core_names = (
            set(CanonicalRecord.model_fields) - {"attrs", "confidence", "review_status"}
        ) | {"source_page", "table_id"}
        failures: list[str] = []
        for entry in truth.expected_records:
            stated = entry.compared_fields()
            stated.pop("source_document", None)
            core: dict[str, Any] = {"source_page": entry.source_page, "table_id": "oracle"}
            attrs: dict[str, Any] = {}
            for name, value in stated.items():
                if name in ("source_page", "table_id"):
                    continue
                if name in core_names:
                    core[name] = value
                else:
                    attrs[name] = value
            if (status := core.get("filing_status")) is not None:
                core["filing_status"] = FilingStatus(str(status))
            for numeric in ("rate", "amount"):
                if core.get(numeric) is not None:
                    core[numeric] = Decimal(str(core[numeric]))
            try:
                CanonicalRecord.model_validate({**core, "attrs": attrs, "confidence": Decimal(1)})
            except Exception as error:  # broad on purpose: name every failure
                failures.append(f"{entry.source_document}/{natural_key(entry)}: {error}")
        assert failures == []

    def test_no_expected_value_is_a_float(self, truth: GroundTruth) -> None:
        """Every oracle number must have arrived as a Decimal built from the
        JSON source text — one float would make exact comparison a lie."""
        for record in truth.expected_records:
            for name, value in record.compared_fields().items():
                assert not isinstance(value, float), (record.source_document, name)


class TestNaturalKey:
    def test_expected_and_actual_agree_on_a_bracket(self) -> None:
        assert natural_key(expected()) == natural_key(actual())

    def test_sub_discriminator_comes_from_the_record_types_own_field(self) -> None:
        entry = expected(
            record_type=RecordType.EMPLOYMENT_TAX_RATE,
            component="synthetic_component",
            lower_bound=None,
            upper_bound=None,
            filing_status=None,
            rate=None,
        )
        assert entry.attribute_key == "synthetic_component"
        assert natural_key(entry).attribute_key == "synthetic_component"

    def test_record_type_without_a_sub_discriminator_keys_on_none(self) -> None:
        assert natural_key(expected()).attribute_key is None

    def test_none_components_survive(self) -> None:
        entry = expected(
            record_type=RecordType.SALES_TAX_RATE,
            tax_year=None,
            filing_status=None,
            taxpayer_class=None,
            lower_bound=None,
            upper_bound=None,
            rate=None,
            currency=None,
        )
        key = natural_key(entry)
        assert key.tax_year is None
        assert key.filing_status is None
        assert key.lower_bound is None


class TestCompare:
    def test_extraction_provenance_and_document_label_are_different_key_spaces(self) -> None:
        # The oracle's table_id is the label the document prints; the mapped
        # record's table_id is extraction provenance. They can never be equal
        # on real data, so the comparison reads the label from attrs.
        result = compare(
            [expected(table_id="table_1")],
            [actual(table_id="p1_t0", attrs={"source_table_label": "table_1"})],
        )
        assert len(result.matched) == 1

    def test_wrong_table_label_is_still_caught(self) -> None:
        result = compare(
            [expected(table_id="table_1")],
            [actual(table_id="p1_t0", attrs={"source_table_label": "table_3"})],
        )
        assert result.field_mismatches
        assert any(d.field == "table_id" for d in result.field_mismatches[0].diffs)

    def test_oracle_commentary_fields_are_not_compared(self) -> None:
        # 'note'/'extraction_note' are free prose documenting traps; a mapper
        # cannot reproduce English sentences and must not be judged on them.
        entry = expected(note="the dash means no tax imposed")
        assert "note" not in entry.compared_fields()
        assert len(compare([entry], [actual()]).matched) == 1

    def test_exact_match(self) -> None:
        result = compare([expected()], [actual()])
        assert len(result.matched) == 1
        assert result.field_mismatches == ()
        assert result.missing == ()
        assert result.spurious == ()
        assert result.is_perfect

    def test_numeric_scale_difference_is_not_a_mismatch(self) -> None:
        """numeric(14,2) round-trips 700 as 700.00; that is the same value."""
        result = compare(
            [expected(amount=700, lower_bound=None, upper_bound=None)],
            [actual(amount=Decimal("700.00"), lower_bound=None, upper_bound=None)],
        )
        assert len(result.matched) == 1

    def test_one_field_off_reports_both_values(self) -> None:
        result = compare([expected()], [actual(rate=Decimal("0.12"))])
        assert result.matched == ()
        assert len(result.field_mismatches) == 1
        mismatch = result.field_mismatches[0]
        assert [d.field for d in mismatch.diffs] == ["rate"]
        assert mismatch.diffs[0].expected == Decimal("0.11")
        assert mismatch.diffs[0].actual == Decimal("0.12")
        assert result.fields_differing == 1
        assert result.fields_compared > 1

    def test_open_ended_top_bracket_matches_on_none(self) -> None:
        result = compare(
            [expected(lower_bound=700, upper_bound=None)],
            [actual(lower_bound=700, upper_bound=None)],
        )
        assert len(result.matched) == 1

    def test_missing_record(self) -> None:
        result = compare([expected()], [])
        assert result.missing == (natural_key(expected()),)
        assert result.matched == ()
        assert result.expected_count == 1

    def test_spurious_record(self) -> None:
        result = compare([], [actual()])
        assert result.spurious == (natural_key(actual()),)
        assert result.expected_count == 0

    def test_key_difference_reads_as_missing_plus_spurious(self) -> None:
        result = compare([expected()], [actual(lower_bound=1, upper_bound=700)])
        assert len(result.missing) == 1
        assert len(result.spurious) == 1
        assert result.field_mismatches == ()

    def test_absent_attribute_is_not_a_null_attribute(self) -> None:
        """A mapper that never produced the field must not read as one that
        correctly produced NULL (anti-goal #8)."""
        result = compare([expected(synthetic_pct=None)], [actual()])
        assert len(result.field_mismatches) == 1
        diff = result.field_mismatches[0].diffs[0]
        assert diff.field == "synthetic_pct"
        assert diff.expected is None
        assert diff.actual is ABSENT

    def test_attrs_carry_type_specific_fields(self) -> None:
        result = compare(
            [expected(prior_year_amount=650)],
            [actual(attrs={"prior_year_amount": 650})],
        )
        assert len(result.matched) == 1

    def test_boolean_is_not_compared_as_an_integer(self) -> None:
        result = compare(
            [expected(synthetic_flag=True)],
            [actual(attrs={"synthetic_flag": 1})],
        )
        assert len(result.field_mismatches) == 1

    def test_lifecycle_status_is_compared_even_when_the_oracle_is_silent(self) -> None:
        """Absent means active; a mapper marking a live record superseded must
        fail, since a superseded record vanishes from tax_year queries."""
        result = compare([expected()], [actual(lifecycle_status=LifecycleStatus.SUPERSEDED)])
        assert len(result.field_mismatches) == 1
        assert result.field_mismatches[0].diffs[0].field == "lifecycle_status"

    def test_unstated_extra_attrs_are_not_penalised(self) -> None:
        result = compare([expected()], [actual(attrs={"raw_cell": "11.0%"})])
        assert len(result.matched) == 1

    def test_duplicate_actual_key_keeps_the_first_and_flags_the_rest(self) -> None:
        """Mirrors the DDL: the natural key is UNIQUE NULLS NOT DISTINCT, so a
        second record under one key could never persist."""
        result = compare([expected()], [actual(), actual(rate=Decimal("0.12"))])
        assert len(result.matched) == 1
        assert result.duplicate_actual_keys == (natural_key(actual()),)
        assert result.spurious == (natural_key(actual()),)

    def test_duplicate_actual_key_is_order_stable(self) -> None:
        first = compare([expected()], [actual(rate=Decimal("0.12")), actual()])
        assert first.matched == ()
        assert len(first.field_mismatches) == 1
        assert len(first.duplicate_actual_keys) == 1

    def test_duplicate_expected_key_is_a_hard_error(self) -> None:
        with pytest.raises(ValueError, match="duplicate natural key"):
            compare([expected(), expected(rate=Decimal("0.12"))], [])


class TestFormatReport:
    def test_perfect_run_reports_accuracy_and_no_failures(self) -> None:
        report = format_report(compare([expected()], [actual()]))
        assert "accuracy by document" in report
        assert "field-level accuracy: 1/1" in report
        assert SYNTHETIC_DOC in report
        assert "TOTAL" in report
        assert "failing records" not in report

    def test_failures_are_named_with_their_reason(self) -> None:
        result = compare(
            [expected(), expected(lower_bound=701, upper_bound=None)],
            [actual(rate=Decimal("0.12")), actual(lower_bound=900, upper_bound=None)],
        )
        report = format_report(result)
        assert "field-level accuracy: 0/2" in report
        assert "failing records (3):" in report
        assert "[field mismatch]" in report
        assert "rate: expected 0.11, actual 0.12" in report
        assert "[missing] 99_synthetic.pdf | ordinary_income_bracket" in report
        assert "[spurious]" in report

    def test_grouping_by_record_type(self) -> None:
        result = compare(
            [
                expected(),
                expected(
                    record_type=RecordType.SALES_TAX_RATE,
                    tax_year=None,
                    filing_status=None,
                    taxpayer_class=None,
                    lower_bound=None,
                    upper_bound=None,
                    rate=None,
                    currency=None,
                ),
            ],
            [actual()],
        )
        by_type = format_report(result, by="record_type")
        assert "accuracy by record_type" in by_type
        assert "ordinary_income_bracket" in by_type
        assert "sales_tax_rate" in by_type
        assert "field-level accuracy: 1/2" in by_type

        # Both records come from one document, so the document view has one
        # table row for it (the failure listing below the table repeats the
        # name once per failing record and is not part of the grouping).
        table = format_report(result, by="document").split("\n\nfailing records")[0]
        assert table.count(SYNTHETIC_DOC) == 1

    def test_empty_comparison_renders(self) -> None:
        report = format_report(compare([], []))
        assert "field-level accuracy: 0/0" in report


def test_end_to_end_accuracy() -> None:
    pytest.skip(
        reason=(
            "Phase 2b: SchemaMapper adapter does not exist yet; the accuracy "
            "table only ever comes from a run against the real mapping API — "
            "never from stubs (anti-goals #1/#8)."
        )
    )


def _code_strings(tree: ast.Module) -> list[str]:
    """Every identifier and non-docstring string literal in a module.

    Docstrings and comments are excluded on purpose: anti-goal #1 forbids a
    module under ``src/`` *importing, opening, or embedding values from* the
    oracle, not mentioning it in prose. String literals other than docstrings
    are kept, so ``open("fixtures/ground_truth.json")`` still trips the check.
    """
    docstring_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    docstring_nodes.add(id(value))

    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str) and id(node) not in docstring_nodes:
                found.append(node.value)
        elif isinstance(node, ast.Name):
            found.append(node.id)
        elif isinstance(node, ast.Attribute):
            found.append(node.attr)
        elif isinstance(node, ast.alias):
            found.append(node.name)
            if node.asname:
                found.append(node.asname)
        elif isinstance(node, ast.ImportFrom):
            found.append(node.module or "")
        elif isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            found.append(node.name)
        elif isinstance(node, ast.arg):
            found.append(node.arg)
        elif isinstance(node, ast.keyword):
            found.append(node.arg or "")
    return found


def test_ground_truth_isolation() -> None:
    """Anti-goal #1, mechanically: the oracle is invisible to ``src/``.

    Every extracted value must be derived from the PDF itself, so no module
    under ``src/`` may import, open, or embed anything from
    ``ground_truth.json``. Only ``tests/accuracy/`` may read it.
    """
    modules = sorted(SRC_DIR.rglob("*.py"))
    assert modules, "no modules found under src/ — the check would pass vacuously"
    offenders: list[str] = []
    for module in modules:
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        if any("ground_truth" in text for text in _code_strings(tree)):
            offenders.append(str(module.relative_to(REPO_ROOT)))
    assert offenders == [], f"src/ modules referencing the test oracle: {offenders}"


def test_ground_truth_isolation_detects_a_violation(tmp_path: Path) -> None:
    """The isolation check must be able to fail — a scanner that never trips
    proves nothing."""
    violation = ast.parse('"""Docstring mentioning ground_truth is fine."""\n')
    assert not any("ground_truth" in text for text in _code_strings(violation))

    for source in (
        'import json\ndata = json.load(open("fixtures/ground_truth.json"))\n',
        "from fixtures import ground_truth\n",
        "GROUND_TRUTH = fixtures.ground_truth\n",
    ):
        tree = ast.parse(source)
        assert any("ground_truth" in text for text in _code_strings(tree)), source
