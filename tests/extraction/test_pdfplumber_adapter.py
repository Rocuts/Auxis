"""Structural tests for the deterministic extractor against the real PDFs.

Every expectation below was read off the fixture documents themselves —
never from ``fixtures/ground_truth.json``, which is the accuracy oracle and
off limits outside ``tests/accuracy/`` (anti-goal #1). These assertions pin
*shape and fidelity*: the right number of tables, the right grid dimensions,
merged-cell ``None``s preserved, em dashes and negatives untouched, and the
prose sentences that carry facts no table states.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tax_tables.adapters.pdfplumber_extractor import PdfplumberExtractor
from tax_tables.extraction.model import (
    ExtractedTable,
    ExtractionMethod,
    GridSource,
    PageExtraction,
    ProseKind,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"

DOC_01 = "01_federal_income_tax_rate_schedules_TY2026.pdf"
DOC_02 = "02_standard_deduction_schedule_TY2026.pdf"
DOC_03 = "03_state_local_sales_tax_rates_2026.pdf"
DOC_04 = "04_employment_tax_rates_and_thresholds_2026.pdf"


def extract(name: str, *pages: int) -> list[PageExtraction]:
    batch = PdfplumberExtractor().extract_pages((FIXTURES / name).read_bytes(), pages)
    # $0 by construction, asserted on every single extraction in this file.
    assert batch.api_calls == 0
    assert batch.usd == 0
    return batch.pages


def texts(table: ExtractedTable, row: int) -> list[str | None]:
    return [cell.text for cell in table.rows[row]]


def dims(table: ExtractedTable) -> tuple[int, int]:
    return table.row_count, table.column_count


def prose_text(page: PageExtraction) -> str:
    return "\n".join(block.text for block in page.prose)


# Module-scoped so each PDF is parsed once for the whole file.


@pytest.fixture(scope="module")
def page_01() -> PageExtraction:
    return extract(DOC_01, 1)[0]


@pytest.fixture(scope="module")
def page_02() -> PageExtraction:
    return extract(DOC_02, 1)[0]


@pytest.fixture(scope="module")
def pages_03() -> list[PageExtraction]:
    return extract(DOC_03, 1, 2)


@pytest.fixture(scope="module")
def page_04() -> PageExtraction:
    return extract(DOC_04, 1)[0]


class TestAdapterContract:
    def test_engine_name(self) -> None:
        assert PdfplumberExtractor().engine == "pdfplumber"

    def test_returns_exactly_the_requested_pages_in_order(self) -> None:
        pages = extract(DOC_03, 2, 1)
        assert [page.page_number for page in pages] == [2, 1]

    def test_every_page_is_marked_deterministic(self) -> None:
        pages = extract(DOC_03, 1, 2)
        assert {page.method for page in pages} == {ExtractionMethod.DETERMINISTIC_TEXT}

    def test_out_of_range_page_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            extract(DOC_01, 2)

    def test_table_ids_are_stable_and_page_scoped(self) -> None:
        page = extract(DOC_04, 1)[0]
        assert [table.table_id for table in page.tables] == ["p1_t0", "p1_t1", "p1_t2", "p1_t3"]


class TestDocument01WideMatrix:
    """Two-level header with a merged cell; one visual row is four records."""

    def test_finds_both_tables(self, page_01: PageExtraction) -> None:
        assert len(page_01.tables) == 2

    def test_rate_schedule_is_nine_by_five(self, page_01: PageExtraction) -> None:
        assert dims(page_01.tables[0]) == (9, 5)
        assert page_01.tables[0].grid_source is GridSource.RULED_LINES

    def test_merged_header_keeps_none_apart_from_empty(self, page_01: PageExtraction) -> None:
        # "" is a real empty cell (above "Rate"); None marks the three
        # positions the merged "Taxable Income Bracket" label spans.
        assert texts(page_01.tables[0], 0) == ["", "Taxable Income Bracket", None, None, None]

    def test_wrapped_header_label_is_joined_by_a_single_space(
        self, page_01: PageExtraction
    ) -> None:
        assert "Married Filing Jointly" in texts(page_01.tables[0], 1)

    def test_open_ended_top_bracket_survives_verbatim(self, page_01: PageExtraction) -> None:
        assert "$643,251 and over" in texts(page_01.tables[0], 8)

    def test_estates_and_trusts_table_is_five_by_two(self, page_01: PageExtraction) -> None:
        assert dims(page_01.tables[1]) == (5, 2)

    def test_source_note_is_a_footnote_naming_the_open_ended_bracket(
        self, page_01: PageExtraction
    ) -> None:
        footnotes = [b.text for b in page_01.prose if b.kind is ProseKind.FOOTNOTE]
        assert any("open-ended" in text for text in footnotes)

    def test_tax_year_is_stated_in_prose(self, page_01: PageExtraction) -> None:
        assert "Tax Year 2026" in prose_text(page_01)


class TestDocument02BorderlessTables:
    """Row rects but no vertical rules: the ruled grid collapses to one
    column and must be rebuilt from word geometry."""

    def test_basic_deduction_table_is_rebuilt_to_four_columns(
        self, page_02: PageExtraction
    ) -> None:
        table = page_02.tables[0]
        assert table.grid_source is GridSource.WORD_GAP_REBUILD
        assert dims(table) == (5, 4)
        assert table.irregular_row_indexes == []

    def test_rebuilt_row_keeps_current_and_prior_year_columns(
        self, page_02: PageExtraction
    ) -> None:
        # Both year columns must survive: keeping one silently loses half
        # the records this document carries.
        assert texts(page_02.tables[0], 0) == ["Single", "15,400", "15,000", "+400"]

    def test_additional_deduction_table_is_rebuilt_to_two_columns(
        self, page_02: PageExtraction
    ) -> None:
        table = page_02.tables[1]
        assert table.grid_source is GridSource.WORD_GAP_REBUILD
        assert dims(table) == (2, 2)
        assert texts(table, 1) == ["Married, per qualifying spouse", "1,650"]

    def test_effective_date_sentence_is_captured(self, page_02: PageExtraction) -> None:
        # The only place tax_year 2026 is stated; the document id says 2025.
        assert "January 1, 2026" in prose_text(page_02)

    def test_dependent_rule_exists_only_in_prose(self, page_02: PageExtraction) -> None:
        assert "greater of 1,400" in prose_text(page_02)

    def test_lettered_notes_are_classified_as_footnotes(self, page_02: PageExtraction) -> None:
        footnotes = [b.text for b in page_02.prose if b.kind is ProseKind.FOOTNOTE]
        assert any(text.startswith("(a)") for text in footnotes)
        assert any("(b) Amounts in Section 3" in text for text in footnotes)


class TestDocument03TwoPageRoster:
    """51 jurisdictions split across two pages, repeated header, em dash for
    "no tax imposed" (NULL, not zero) and one legitimate negative rate."""

    def test_both_pages_rebuild_to_five_columns(self, pages_03: list[PageExtraction]) -> None:
        assert dims(pages_03[0].tables[0]) == (28, 5)
        assert dims(pages_03[1].tables[0]) == (25, 5)
        assert {p.tables[0].grid_source for p in pages_03} == {GridSource.WORD_GAP_REBUILD}

    def test_fifty_one_jurisdictions_stitch_across_the_page_break(
        self, pages_03: list[PageExtraction]
    ) -> None:
        # Each page repeats the header row; everything else is a state.
        data_rows = [
            row for page in pages_03 for row in page.tables[0].rows if row[0].text != "State"
        ]
        assert len(data_rows) == 51

    def test_em_dash_is_preserved_not_converted_to_zero(
        self, pages_03: list[PageExtraction]
    ) -> None:
        alaska = next(r for r in pages_03[0].tables[0].rows if r[0].text == "Alaska")
        assert [cell.text for cell in alaska] == ["Alaska", "—", "1.821", "1.821", "47"]

    def test_negative_local_rate_survives(self, pages_03: list[PageExtraction]) -> None:
        new_jersey = next(r for r in pages_03[1].tables[0].rows if r[0].text == "New Jersey")
        assert "-0.030" in [cell.text for cell in new_jersey]

    def test_rate_unit_exists_only_in_body_text(self, pages_03: list[PageExtraction]) -> None:
        assert "expressed as percentages" in prose_text(pages_03[0])

    def test_continuation_heading_is_captured_on_page_two(
        self, pages_03: list[PageExtraction]
    ) -> None:
        assert "(continued)" in prose_text(pages_03[1])

    def test_rebate_note_explains_the_negative_rate(self, pages_03: list[PageExtraction]) -> None:
        footnotes = [b.text for b in pages_03[1].prose if b.kind is ProseKind.FOOTNOTE]
        assert any("statutory rebate" in text for text in footnotes)


class TestDocument04LandscapeMultiTable:
    """Four separate tables on one landscape page, mixed units, and a rate
    that appears only in a sentence."""

    def test_page_is_landscape(self, page_04: PageExtraction) -> None:
        assert page_04.width > page_04.height

    def test_four_tables_with_distinct_shapes(self, page_04: PageExtraction) -> None:
        assert [dims(t) for t in page_04.tables] == [(4, 4), (6, 2), (5, 3), (7, 3)]

    def test_wage_base_table_keeps_no_limit_as_a_value(self, page_04: PageExtraction) -> None:
        cells = [cell.text for row in page_04.tables[2].rows for cell in row]
        assert "No limit" in cells

    def test_wage_base_row_keeps_both_tax_years(self, page_04: PageExtraction) -> None:
        cells = [cell.text for row in page_04.tables[2].rows for cell in row]
        assert "$181,800" in cells
        assert "$176,100" in cells

    def test_surtax_rate_exists_only_in_prose(self, page_04: PageExtraction) -> None:
        assert "0.9 percent" in prose_text(page_04)
