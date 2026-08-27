"""Word-geometry -> grid reconstruction, shared by both extraction paths.

pdfplumber's default lines strategy collapses borderless whitespace-aligned
tables (docs 02 and 03) into one-column grids; an OCR engine returns loose
word boxes. Both cases reduce to the same problem: given words with
bounding boxes, rebuild rows and columns.

The column algorithm is the classic whitespace-projection approach, kept
deliberately lightweight (validated against 2026 practice: the intra-cell
vs inter-column gap separation on this corpus is ~10x, so a threshold found
by the largest jump in the sorted gap distribution needs no tuning):

1. pool the horizontal gaps between adjacent words across all rows;
2. split the gap distribution at its largest multiplicative jump — gaps
   above the split are column separators, below are word spacing;
3. a separator must be corroborated *globally*: an x-region only becomes a
   column cut if almost every row is blank there (a single wide-set row
   cannot mint a column);
4. every row is then cast onto the shared cuts. A word that genuinely
   straddles a cut (a header label spanning data columns) marks its row
   irregular — flagged, never force-fit or dropped.

Everything here is pure geometry: no pdfplumber, no tesseract, fully
unit-testable with synthetic words.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from statistics import median

from tax_tables.extraction.model import ONE, Cell

#: A gap distribution whose largest jump is below this ratio is considered
#: homogeneous — all word spacing, no column structure.
_MIN_JUMP_RATIO = 3.0
#: Fraction of rows allowed to have ink inside a column separator region
#: (tolerates a straddling header row without closing the gap).
_COVERAGE_TOLERANCE = 0.2
#: A word must protrude this far (in x units) past a cut on both sides to
#: count as straddling rather than merely touching.
_STRADDLE_SLACK = 2.0


@dataclass(frozen=True)
class Word:
    """One positioned word. Coordinates are any consistent unit (PDF points
    or image pixels); ``confidence`` is 0..1, digital text is 1."""

    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    confidence: Decimal = ONE

    @property
    def height(self) -> float:
        return self.bottom - self.top


def group_rows(words: list[Word], *, tolerance_factor: float = 0.5) -> list[list[Word]]:
    """Cluster words into visual rows by vertical position.

    A word starts a new row when its top sits below the current row's
    running top by more than ``tolerance_factor`` x the median word height.
    Rows come back top-to-bottom, words within a row left-to-right.
    """
    if not words:
        return []
    tolerance = median(w.height for w in words) * tolerance_factor
    rows: list[list[Word]] = []
    current: list[Word] = []
    current_top = 0.0
    for word in sorted(words, key=lambda w: (w.top, w.x0)):
        if not current:
            current = [word]
            current_top = word.top
        elif word.top - current_top <= tolerance:
            current.append(word)
        else:
            rows.append(sorted(current, key=lambda w: w.x0))
            current = [word]
            current_top = word.top
    rows.append(sorted(current, key=lambda w: w.x0))
    return rows


def _gap_threshold(rows: list[list[Word]]) -> float | None:
    """Split the pooled gap distribution at its largest multiplicative jump.

    Returns the threshold above which a gap is a column separator, or None
    when the distribution is homogeneous (single-column table).
    """
    gaps = sorted(max(nxt.x0 - prev.x1, 0.1) for row in rows for prev, nxt in pairwise(row))
    if not gaps:
        return None
    best_ratio, best_index = 0.0, -1
    for i in range(len(gaps) - 1):
        ratio = gaps[i + 1] / gaps[i]
        if ratio >= best_ratio:
            # >= : prefer the rightmost equally-large jump, so ties resolve
            # toward fewer, wider separators.
            best_ratio, best_index = ratio, i
    if best_index < 0 or best_ratio < _MIN_JUMP_RATIO:
        return None
    return math.sqrt(gaps[best_index] * gaps[best_index + 1])


def column_cuts(
    rows: list[list[Word]], *, coverage_tolerance: float = _COVERAGE_TOLERANCE
) -> list[float]:
    """Infer global column cut positions from word x-intervals.

    Only x-regions at least as wide as the gap threshold and blank in at
    least ``1 - coverage_tolerance`` of the rows become cuts. Returns cut
    x-positions (empty list means single column).
    """
    occupied = [row for row in rows if row]
    threshold = _gap_threshold(occupied)
    if threshold is None:
        return []
    x_min = min(row[0].x0 for row in occupied)
    x_max = max(row[-1].x1 for row in occupied)
    step = threshold / 8  # fine enough that no separator is stepped over
    max_ink = int(len(occupied) * coverage_tolerance)

    def ink_rows(x: float) -> int:
        return sum(1 for row in occupied if any(w.x0 <= x <= w.x1 for w in row))

    cuts: list[float] = []
    run_start: float | None = None
    x = x_min
    while x <= x_max:
        blank = ink_rows(x) <= max_ink
        if blank and run_start is None:
            run_start = x
        elif not blank and run_start is not None:
            if x - run_start >= threshold and run_start > x_min:
                cuts.append((run_start + x) / 2)
            run_start = None
        x += step
    # A trailing blank run touches the table edge: not a separator.
    return cuts


def build_grid(rows: list[list[Word]], cuts: list[float]) -> tuple[list[list[Cell]], list[int]]:
    """Cast rows onto shared column cuts.

    Returns (grid, irregular_row_indexes). The grid is rectangular with
    ``len(cuts) + 1`` columns; a row whose words straddle a cut is flagged
    irregular but still rendered faithfully by maximum overlap.
    """
    column_count = len(cuts) + 1
    grid: list[list[Cell]] = []
    irregular: list[int] = []
    for row_index, row in enumerate(rows):
        buckets: list[list[Word]] = [[] for _ in range(column_count)]
        straddles = False
        for word in row:
            spans = [i for i in range(column_count) if _overlap(word, cuts, i) > _STRADDLE_SLACK]
            if len(spans) > 1:
                straddles = True
            target = max(range(column_count), key=lambda i: _overlap(word, cuts, i))
            buckets[target].append(word)
        if straddles:
            irregular.append(row_index)
        grid.append(
            [
                Cell(
                    text=" ".join(w.text for w in bucket),
                    confidence=min((w.confidence for w in bucket), default=ONE),
                )
                if bucket
                else Cell(text="")
                for bucket in buckets
            ]
        )
    return grid, irregular


def _overlap(word: Word, cuts: list[float], column: int) -> float:
    lo = cuts[column - 1] if column > 0 else float("-inf")
    hi = cuts[column] if column < len(cuts) else float("inf")
    return max(0.0, min(word.x1, hi) - max(word.x0, lo))


@dataclass(frozen=True)
class Line:
    """One visual text line, for prose grouping."""

    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    confidence: Decimal = ONE


def rows_to_lines(rows: list[list[Word]]) -> list[Line]:
    return [
        Line(
            text=" ".join(w.text for w in row),
            x0=min(w.x0 for w in row),
            x1=max(w.x1 for w in row),
            top=min(w.top for w in row),
            bottom=max(w.bottom for w in row),
            confidence=min(w.confidence for w in row),
        )
        for row in rows
        if row
    ]


def group_lines_into_blocks(lines: list[Line], *, gap_factor: float = 1.8) -> list[list[Line]]:
    """Group consecutive lines into visual blocks: a vertical gap larger
    than ``gap_factor`` x the median line height starts a new block."""
    if not lines:
        return []
    ordered = sorted(lines, key=lambda ln: ln.top)
    threshold = median(ln.bottom - ln.top for ln in ordered) * gap_factor
    blocks: list[list[Line]] = [[ordered[0]]]
    for line in ordered[1:]:
        if line.top - blocks[-1][-1].bottom > threshold:
            blocks.append([line])
        else:
            blocks[-1].append(line)
    return blocks
