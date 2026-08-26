"""Grid reconstruction unit tests on synthetic word geometry.

The layouts mirror the two shapes that defeat pdfplumber's default
strategy: doc 02 (multi-word label column, whitespace-aligned numeric
columns) and doc 03 (a header label straddling a data-column boundary).
"""

from __future__ import annotations

from decimal import Decimal

from tax_tables.extraction.gridbuild import (
    Line,
    Word,
    build_grid,
    column_cuts,
    group_lines_into_blocks,
    group_rows,
    rows_to_lines,
)


def word(text: str, x0: float, x1: float, top: float, conf: str = "1") -> Word:
    return Word(text=text, x0=x0, x1=x1, top=top, bottom=top + 10, confidence=Decimal(conf))


def deduction_rows() -> list[list[Word]]:
    """Four rows shaped like doc 02: label words 4pt apart, columns ~40pt
    apart, labels of different lengths."""
    rows = []
    specs = [
        [("Single", 0, 30)],
        [("Married", 0, 35), ("filing", 39, 63), ("jointly", 67, 95)],
        [("Head", 0, 22), ("of", 26, 34), ("household", 38, 84)],
        [("Qualifying", 0, 48), ("spouse", 52, 80)],
    ]
    amounts = [
        ("15,400", "15,000", "+400"),
        ("30,800", "30,000", "+800"),
        ("23,100", "22,500", "+600"),
        ("30,800", "30,000", "+800"),
    ]
    for i, (label, nums) in enumerate(zip(specs, amounts, strict=True)):
        top = i * 20.0
        row = [word(t, x0, x1, top) for t, x0, x1 in label]
        for j, value in enumerate(nums):
            x0 = 140 + j * 60
            row.append(word(value, x0, x0 + 28, top))
        rows.append(row)
    return rows


class TestGroupRows:
    def test_clusters_by_vertical_position(self) -> None:
        flat = [w for row in deduction_rows() for w in row]
        assert [len(r) for r in group_rows(flat)] == [4, 6, 6, 5]

    def test_orders_rows_top_to_bottom_and_words_left_to_right(self) -> None:
        rows = group_rows([word("b", 50, 60, 0), word("a", 0, 10, 1), word("c", 0, 10, 30)])
        assert [[w.text for w in row] for row in rows] == [["a", "b"], ["c"]]

    def test_empty_input(self) -> None:
        assert group_rows([]) == []


class TestColumnCuts:
    def test_finds_four_columns_in_deduction_layout(self) -> None:
        assert len(column_cuts(deduction_rows())) == 3

    def test_homogeneous_gaps_mean_single_column(self) -> None:
        # A sentence: all gaps are word spacing, no column structure.
        texts = ["a", "b", "c", "d", "e"]
        row = [word(t, i * 30.0, i * 30.0 + 26, 0) for i, t in enumerate(texts)]
        assert column_cuts([row]) == []

    def test_single_wide_set_row_cannot_mint_a_column(self) -> None:
        # Nine tight rows, one row with a huge internal gap: global
        # corroboration must refuse the separator.
        rows = [[word("aa", 0, 40, i * 20.0), word("bb", 44, 90, i * 20.0)] for i in range(9)]
        rows.append([word("aa", 0, 10, 180.0), word("bb", 80, 90, 180.0)])
        assert column_cuts(rows) == []


class TestBuildGrid:
    def test_rectangular_grid_with_faithful_text(self) -> None:
        rows = deduction_rows()
        grid, irregular = build_grid(rows, column_cuts(rows))
        assert irregular == []
        assert [c.text for c in grid[1]] == ["Married filing jointly", "30,800", "30,000", "+800"]

    def test_missing_value_becomes_empty_cell_not_a_shift(self) -> None:
        rows = deduction_rows()
        rows[2] = [w for w in rows[2] if w.text != "22,500"]
        grid, _ = build_grid(rows, column_cuts(deduction_rows()))
        assert [c.text for c in grid[2]] == ["Head of household", "23,100", "", "+600"]

    def test_straddling_header_is_flagged_irregular_never_dropped(self) -> None:
        rows = deduction_rows()
        # A header word sitting across the label/first-amount boundary.
        header = [
            word("Filing", 0, 30, -20),
            word("status", 34, 60, -20),
            word("Amounts", 100, 175, -20),
        ]
        cuts = column_cuts(rows)
        grid, irregular = build_grid([header, *rows], cuts)
        assert irregular == [0]
        assert "Amounts" in " ".join(c.text or "" for c in grid[0])

    def test_cell_confidence_is_min_of_word_confidences(self) -> None:
        row = [
            word("48,351", 0, 30, 0, "0.96"),
            word("to", 34, 44, 0, "0.61"),
            word("533,400", 48, 90, 0, "0.94"),
            word("x", 200, 210, 0, "0.99"),
        ]
        grid, _ = build_grid([row], [150.0])
        assert grid[0][0].confidence == Decimal("0.61")


class TestProseGrouping:
    def test_rows_to_lines_joins_and_bounds(self) -> None:
        lines = rows_to_lines(group_rows([word("a", 0, 10, 0), word("b", 14, 24, 1)]))
        assert lines[0].text == "a b"
        assert (lines[0].x0, lines[0].x1) == (0, 24)

    def test_blocks_split_on_large_vertical_gap(self) -> None:
        def mk(top: float) -> Line:
            return Line(text="x", x0=0, x1=10, top=top, bottom=top + 10)

        blocks = group_lines_into_blocks([mk(0), mk(12), mk(24), mk(80), mk(92)])
        assert [len(b) for b in blocks] == [3, 2]
