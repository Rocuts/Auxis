"""Confidence semantics of the extracted-grid model.

The aggregates are tail-based on purpose: a mean would reward an extractor
that drops the hard table and averages the easy prose (anti-goal #8 by
metric design). These tests pin that choice.
"""

from __future__ import annotations

from decimal import Decimal

from tax_tables.extraction.model import (
    Cell,
    ExtractedDocument,
    ExtractedTable,
    ExtractionCost,
    ExtractionMethod,
    GridSource,
    OcrPageStats,
    PageExtraction,
    percentile,
)


def table(cells: list[list[Cell]], irregular: list[int] | None = None) -> ExtractedTable:
    return ExtractedTable(
        page_number=1,
        table_id="p1_t0",
        bbox=(0, 0, 100, 100),
        grid_source=GridSource.RULED_CELL_OCR,
        rows=cells,
        column_count=max((len(r) for r in cells), default=1),
        irregular_row_indexes=irregular or [],
    )


def page(tables: list[ExtractedTable], stats: OcrPageStats | None = None) -> PageExtraction:
    return PageExtraction(
        page_number=1,
        width=612,
        height=792,
        method=ExtractionMethod.OCR if stats else ExtractionMethod.DETERMINISTIC_TEXT,
        tables=tables,
        ocr_stats=stats,
    )


def document(pages: list[PageExtraction]) -> ExtractedDocument:
    return ExtractedDocument(
        filename="f.pdf",
        sha256="0" * 64,
        pages=pages,
        cost=ExtractionCost(engine="test", wall_seconds=0),
    )


class TestPercentile:
    def test_nearest_rank_rounds_down(self) -> None:
        values = [Decimal(i) for i in range(1, 11)]
        assert percentile(values, Decimal("0.1")) == Decimal(1)
        assert percentile(values, Decimal("0.5")) == Decimal(5)


class TestTableConfidence:
    def test_uses_tail_not_mean(self) -> None:
        # Ninety perfect cells and ten mediocre ones: the mean would say
        # 0.95; the p10 tail reports what the shaky tenth actually reads at.
        rows = [[Cell(text="v") for _ in range(10)] for _ in range(9)]
        rows.append([Cell(text="v", confidence=Decimal("0.5")) for _ in range(10)])
        assert table(rows).confidence == Decimal("0.5")

    def test_a_few_lost_cells_cannot_hide_in_the_tail(self) -> None:
        # One unreadable cell in twenty is too rare to move the p10, but a
        # lost cell must never report as a perfect table: the flagged
        # fraction caps the aggregate.
        rows = [[Cell(text="v") for _ in range(10)] for _ in range(2)]
        rows[1][9] = Cell(text="", confidence=Decimal(0), ink_without_text=True)
        assert table(rows).confidence == Decimal("0.95")

    def test_merged_continuations_do_not_dilute(self) -> None:
        # None cells are colspan continuations, not evidence of quality.
        rows = [[Cell(text="head"), Cell(text=None), Cell(text=None)]]
        assert table(rows).confidence == Decimal(1)

    def test_irregular_rows_scale_confidence_down(self) -> None:
        rows = [[Cell(text="a"), Cell(text="b")] for _ in range(4)]
        assert table(rows, irregular=[0]).confidence == Decimal("0.75")

    def test_empty_grid_is_zero(self) -> None:
        assert table([]).confidence == Decimal(0)

    def test_flagged_cell_count(self) -> None:
        rows = [[Cell(text="", confidence=Decimal(0), ink_without_text=True), Cell(text="x")]]
        assert table(rows).flagged_cell_count == 1


class TestDocumentAggregates:
    def test_confidence_is_min_over_tables_and_ocr_tails(self) -> None:
        good = table([[Cell(text="x")]])
        stats = OcrPageStats(
            word_count=100,
            mean_confidence=Decimal("0.95"),
            p10_confidence=Decimal("0.7"),
            low_confidence_fraction=Decimal("0.1"),
        )
        doc = document([page([good], stats)])
        assert doc.confidence == Decimal("0.7")

    def test_no_evidence_means_confidence_one(self) -> None:
        assert document([page([])]).confidence == Decimal(1)

    def test_flattening_preserves_page_order(self) -> None:
        t1 = table([[Cell(text="a")]])
        doc = ExtractedDocument(
            filename="f.pdf",
            sha256="0" * 64,
            pages=[
                PageExtraction(
                    page_number=1,
                    width=612,
                    height=792,
                    method=ExtractionMethod.DETERMINISTIC_TEXT,
                    tables=[t1],
                ),
                PageExtraction(
                    page_number=2,
                    width=612,
                    height=792,
                    method=ExtractionMethod.DETERMINISTIC_TEXT,
                    tables=[t1],
                ),
            ],
            cost=ExtractionCost(engine="test", wall_seconds=0),
        )
        assert len(doc.tables) == 2
        assert doc.methods == {ExtractionMethod.DETERMINISTIC_TEXT}
