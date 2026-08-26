"""Structural invariants of the local OCR adapter against fixture 05.

Fixture 05 is the only scanned document in the corpus: no text layer, ~200
DPI, skewed, blurred, JPEG-compressed. Everything here is derived from the
scan itself (the ground truth is the accuracy harness's oracle and is not
readable from anywhere but ``tests/accuracy/`` — anti-goal #1).

The load-bearing assertion is the first column of Table 1. Whole-page OCR at
every segmentation mode *provably* loses those stub cells — 'Rate',
'0 percent', '20 percent' among them — because the ruling lines defeat page
layout analysis. Line-grid detection plus per-cell OCR recovers them at
confidence 0.94-0.96. If that assertion ever fails, the adapter has silently
regressed to a page-level read, which is exactly the quiet data loss
anti-goal #8 forbids. Fix the extractor; never relax this test.

Cell text is asserted exactly (per-cell OCR measured clean). Prose is
asserted by substring: long paragraphs on a noisy scan carry cosmetic noise
that costs no data — the real NOTE block, for instance, reads 'Table |' for
'Table 1' while the rate it uniquely carries, 3.8 percent, comes through
intact.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tax_tables.adapters.tesseract_extractor import TesseractExtractor
from tax_tables.extraction.model import (
    ExtractedTable,
    ExtractionMethod,
    GridSource,
    PageExtraction,
    ProseKind,
)
from tax_tables.ports.extractor import PageBatch

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
SCANNED_DOC = "05_capital_gains_preferential_rates_TY2025.pdf"


@pytest.fixture(scope="module")
def batch() -> PageBatch:
    """One OCR run for the whole module: the pipeline takes seconds per page,
    and every test here reads the same immutable result."""
    return TesseractExtractor().extract_pages((FIXTURES / SCANNED_DOC).read_bytes(), [1])


@pytest.fixture(scope="module")
def page(batch: PageBatch) -> PageExtraction:
    return batch.pages[0]


def _texts(table: ExtractedTable, row: int) -> list[str]:
    return [cell.text or "" for cell in table.rows[row]]


def _column(table: ExtractedTable, column: int) -> list[str]:
    return [(row[column].text or "") for row in table.rows]


class TestCost:
    def test_local_binary_costs_nothing(self, batch: PageBatch) -> None:
        # tesseract is a local binary, not a hosted service: fixture 05 is the
        # only document with nonzero extraction cost on any target, and even
        # that is zero on this one.
        assert batch.usd == 0
        assert batch.api_calls == 0

    def test_engine_name(self) -> None:
        assert TesseractExtractor().engine == "tesseract"


class TestPage:
    def test_page_reports_ocr_method_and_pdf_geometry(self, page: PageExtraction) -> None:
        assert page.method is ExtractionMethod.OCR
        assert (page.page_number, page.width, page.height) == (1, 612.0, 792.0)

    def test_exactly_two_tables(self, page: PageExtraction) -> None:
        assert len(page.tables) == 2

    def test_grids_come_from_the_line_grid(self, page: PageExtraction) -> None:
        assert {t.grid_source for t in page.tables} == {GridSource.RULED_CELL_OCR}


class TestTableOne:
    def test_shape(self, page: PageExtraction) -> None:
        table = page.tables[0]
        assert (table.row_count, table.column_count) == (4, 5)

    def test_stub_column_survives(self, page: PageExtraction) -> None:
        # THE test: these are the cells page-level OCR drops at every psm.
        assert _column(page.tables[0], 0) == ["Rate", "0 percent", "15 percent", "20 percent"]

    def test_header_names_the_filing_statuses(self, page: PageExtraction) -> None:
        header = _texts(page.tables[0], 0)
        assert "Single" in header
        assert "Married Filing Jointly" in header
        assert "Married Filing Separately" in header
        assert "Head of Household" in header

    def test_bracket_bounds_read_verbatim(self, page: PageExtraction) -> None:
        # Verbatim means verbatim: this document separates bounds with "to",
        # not the en dash the other four use, and the extractor must not
        # normalize that away before the mapper sees it.
        cells = [cell.text for row in page.tables[0].rows for cell in row]
        assert "$48,351 to $533,400" in cells
        assert "Over $533,400" in cells

    def test_open_ended_top_bracket_present_for_every_status(self, page: PageExtraction) -> None:
        top = _texts(page.tables[0], 3)[1:]
        assert all(text.startswith("Over $") for text in top), top


class TestTableTwo:
    def test_shape(self, page: PageExtraction) -> None:
        table = page.tables[1]
        assert (table.row_count, table.column_count) == (4, 2)

    @pytest.mark.parametrize(
        ("category", "rate"),
        [
            ("Unrecaptured section 1250 gain", "25 percent"),
            ("Collectibles and certain small business stock", "28 percent"),
            ("Short-term capital gain", "Ordinary rates"),
        ],
    )
    def test_category_rate_pairs(self, page: PageExtraction, category: str, rate: str) -> None:
        pairs = [(_texts(page.tables[1], i)[0], _texts(page.tables[1], i)[1]) for i in range(4)]
        assert (category, rate) in pairs

    def test_header(self, page: PageExtraction) -> None:
        assert _texts(page.tables[1], 0) == ["Category", "Maximum rate"]


class TestCellQuality:
    def test_every_cell_is_confident_or_flagged(self, page: PageExtraction) -> None:
        """No cell may be quietly mediocre. Either the engine read it well, or
        it is flagged ``ink_without_text`` and bound for the review queue —
        there is no third state (anti-goal #8)."""
        suspect = [
            (table.table_id, r, c, cell.text, cell.confidence, cell.ink_without_text)
            for table in page.tables
            for r, row in enumerate(table.rows)
            for c, cell in enumerate(row)
            if cell.confidence <= Decimal("0.5") and not cell.ink_without_text
        ]
        assert not suspect, f"unflagged low-confidence cells: {suspect}"

    def test_no_cell_lost_its_ink(self, page: PageExtraction) -> None:
        # A flagged cell is honest but still a defect; on this fixture the
        # per-cell pipeline reads all 28 cells, so any flag is a regression.
        flagged = [t.table_id for t in page.tables if t.flagged_cell_count]
        assert not flagged, f"tables with ink the engine could not read: {flagged}"


class TestProse:
    def test_superseded_notice_is_captured(self, page: PageExtraction) -> None:
        # Losing this sentence loses the lifecycle_status of the whole
        # document, and doc 05 must never surface in a tax_year=2026 query.
        assert any("SUPERSEDED" in block.text for block in page.prose)

    def test_successor_circular_is_named(self, page: PageExtraction) -> None:
        assert any("CG-2026/03" in block.text for block in page.prose)

    def test_note_block_carries_the_surtax_rate(self, page: PageExtraction) -> None:
        # 3.8 percent exists ONLY here — not in either table. A prose pass
        # that drops footnotes drops a record.
        notes = [b for b in page.prose if b.kind is ProseKind.FOOTNOTE]
        assert notes, "no footnote block classified"
        assert any("3.8 percent" in b.text for b in notes), [b.text for b in notes]

    def test_prose_blocks_are_positioned_in_pdf_points(self, page: PageExtraction) -> None:
        for block in page.prose:
            x0, top, x1, bottom = block.bbox
            assert 0 <= x0 < x1 <= page.width, block.text[:60]
            assert 0 <= top < bottom <= page.height, block.text[:60]

    def test_tax_year_is_stated_in_prose(self, page: PageExtraction) -> None:
        # 'Tax Year 2025' appears in one small block and nowhere in either
        # table. Lose it and doc 05's tax_year is only guessable — from the
        # successor circular's effective date or the document id, both of
        # which the brief forbids as tax_year sources.
        assert any("Tax Year 2025" in block.text for block in page.prose)


class TestOcrStats:
    def test_word_coverage_guards_against_dropout(self, page: PageExtraction) -> None:
        # Measured: this pipeline finds 285 words on the page. The bound sits
        # ~5% below that — above the 263 that losing the smallest prose block
        # would leave, and above every whole-page PSM mode that silently
        # drops table content (a guard at 200 would have waved those through,
        # tolerating a 30% loss while claiming to catch dropout).
        assert page.ocr_stats is not None
        assert page.ocr_stats.word_count > 270

    def test_confidence_tail_is_healthy(self, page: PageExtraction) -> None:
        assert page.ocr_stats is not None
        assert page.ocr_stats.p10_confidence >= Decimal("0.5")
