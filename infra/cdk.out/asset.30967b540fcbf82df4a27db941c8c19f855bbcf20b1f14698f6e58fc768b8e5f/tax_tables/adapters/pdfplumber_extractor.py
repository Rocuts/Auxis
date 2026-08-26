"""Deterministic TableExtractor: pdfplumber over a page's own text layer.

This is the adapter the router reaches for on four of the five fixture
documents, and it is the reason the cost table reads "4 of 5 documents cost
$0 on every target": it makes no API call, on any target, ever. That is a
construction guarantee, not a policy — there is nothing here to bill.

Fidelity rules this adapter holds to, each learned from a fixture:

- ``None`` vs ``""`` in a cell is *content*. pdfplumber emits ``None`` for
  the continuation positions of a merged cell (doc 01's two-level header
  spans one label across four columns) and ``""`` for a cell that exists
  and is empty. The SchemaMapper needs both signals to reconstruct the
  header, so they survive verbatim into ``Cell.text``.
- An embedded newline inside a cell is a *visual* wrap, not content:
  doc 01's header reads ``"Married Filing\\nJointly"`` only because the
  column is narrow. Newlines collapse to a single space; nothing else about
  the text is touched — an em dash stays an em dash (doc 03 uses it for
  "no tax imposed", which is NULL, not zero) and a leading minus survives
  (doc 03's New Jersey rebate is a legitimate negative rate).
- A grid that comes back with one column and multi-token cells is not a
  one-column table: it is a borderless table whose rules pdfplumber could
  not see (docs 02 and 03 draw row rects but no vertical lines). Those are
  rebuilt from word geometry rather than handed downstream as row-shaped
  mush — see ``_looks_degenerate``.

Strategy choices, measured on this corpus rather than assumed:

- ``lines_strict`` returns **zero** tables on doc 03, whose grid is drawn
  with filled rects instead of stroked lines. Not usable.
- A whole-page ``text`` strategy fuses doc 04's four side-by-side tables,
  and the surrounding prose with them, into a single blob. Not usable.
- The default (lines) strategy plus a word-gap rebuild handles all four.

Prose is not a leftover: doc 02's dependent rule, doc 03's rate unit and
doc 04's 0.9% surtax rate exist *only* in sentences outside the tables. Any
word not covered by a table's bbox is collected, grouped into blocks and
shipped with the page (anti-goal #8 — nothing on the page is discarded).
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pdfplumber

from tax_tables.extraction.gridbuild import (
    Word,
    build_grid,
    column_cuts,
    group_lines_into_blocks,
    group_rows,
    rows_to_lines,
)
from tax_tables.extraction.model import (
    ONE,
    Cell,
    ExtractedTable,
    ExtractionMethod,
    GridSource,
    PageExtraction,
    ProseBlock,
)
from tax_tables.extraction.prose import classify_block
from tax_tables.ports.extractor import PageBatch

if TYPE_CHECKING:
    from pdfplumber.page import Page

BBox = tuple[float, float, float, float]

#: A one-column grid is treated as a collapsed borderless table when at
#: least this fraction of its non-empty cells hold several tokens. One
#: multi-word cell proves nothing (a title row); a majority of them means
#: whole source rows were flattened into single cells.
_DEGENERATE_ROW_FRACTION = 0.5
#: Tokens in a cell before it counts as "a whole row squashed into a cell".
#: Three is the floor across this corpus: doc 02's shortest data row is
#: "Single 15,400 15,000 +400" and doc 03's is "Alabama 4.000 5.290 ...".
_DEGENERATE_MIN_TOKENS = 3
#: Fraction of a word's area that must fall inside a table's bbox before the
#: word is considered part of that table rather than prose. Area-based
#: rather than strict containment so a word clipping a bbox edge lands in
#: exactly one place — never both (duplicated) and never neither (dropped).
_IN_TABLE_OVERLAP = 0.5


class PdfplumberExtractor:
    """TableExtractor for pages that have a usable text layer.

    Stateless and cheap to construct; one instance may serve many documents.
    The router's scan-detection (upright-char threshold plus the page-sized-
    image test) keeps scanned pages away from this adapter; were one to slip
    through anyway, it would return empty pages rather than invent content —
    which is why that routing invariant is pinned by its own tests, since an
    empty page here is indistinguishable from a genuinely blank one.
    """

    @property
    def engine(self) -> str:
        return "pdfplumber"

    def extract_pages(self, pdf_bytes: bytes, page_numbers: Sequence[int]) -> PageBatch:
        """Extract exactly the requested 1-based pages.

        Returns one PageExtraction per requested page, in the order asked
        for. ``api_calls``/``usd`` are zero by construction: nothing in this
        path calls a hosted service.
        """
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            page_count = len(pdf.pages)
            for number in page_numbers:
                if not 1 <= number <= page_count:
                    # Loud, not clamped: a caller asking for a page that is
                    # not there has a bug the silent path would hide.
                    raise ValueError(f"page {number} out of range (document has {page_count})")
            pages = [self._extract_page(pdf.pages[number - 1], number) for number in page_numbers]
        return PageBatch(pages=pages, api_calls=0, usd=Decimal(0))

    def _extract_page(self, page: Page, number: int) -> PageExtraction:
        # Reading order: top-to-bottom, then left-to-right. Doc 04 is
        # landscape with two table columns, so x0 is a real tiebreaker.
        found = sorted(page.find_tables(), key=lambda t: (t.bbox[1], t.bbox[0]))
        tables = [
            self._extract_table(page, table, number, index) for index, table in enumerate(found)
        ]
        return PageExtraction(
            page_number=number,
            width=float(page.width),
            height=float(page.height),
            method=ExtractionMethod.DETERMINISTIC_TEXT,
            tables=tables,
            prose=self._extract_prose(page, number, [_bbox(t.bbox) for t in found]),
        )

    def _extract_table(self, page: Page, table: Any, number: int, index: int) -> ExtractedTable:
        raw: list[list[str | None]] = table.extract()
        grid = [[_normalize(value) for value in row] for row in raw]
        bbox = _bbox(table.bbox)

        source = GridSource.RULED_LINES
        irregular: list[int] = []
        if _looks_degenerate(grid):
            rebuilt = self._rebuild_from_words(page, bbox)
            if rebuilt is not None:
                grid, irregular = rebuilt
                source = GridSource.WORD_GAP_REBUILD

        rows = [[Cell(text=value, confidence=ONE) for value in row] for row in grid]
        column_count = max((len(row) for row in rows), default=1)
        # Ragged rows are reported, never padded or trimmed into shape.
        irregular = sorted(
            {*irregular, *(i for i, row in enumerate(rows) if len(row) != column_count)}
        )
        return ExtractedTable(
            page_number=number,
            table_id=f"p{number}_t{index}",
            bbox=bbox,
            grid_source=source,
            rows=rows,
            column_count=column_count,
            irregular_row_indexes=irregular,
        )

    def _rebuild_from_words(
        self, page: Page, bbox: BBox
    ) -> tuple[list[list[str | None]], list[int]] | None:
        """Re-infer columns for a borderless table from word x-geometry.

        ``page.crop`` keeps absolute page coordinates, so the words come out
        in the same frame as the bbox and no offset arithmetic is needed.
        Returns None when the rebuild cannot improve on the ruled grid (no
        words, or no corroborated column separator) — in that case the
        original grid is kept rather than replaced by a worse one.
        """
        words = [
            Word(
                text=str(word["text"]),
                x0=float(word["x0"]),
                x1=float(word["x1"]),
                top=float(word["top"]),
                bottom=float(word["bottom"]),
                confidence=ONE,
            )
            for word in page.crop(bbox).extract_words()
        ]
        if not words:
            return None
        rows = group_rows(words)
        cuts = column_cuts(rows)
        if not cuts:
            return None
        cells, irregular = build_grid(rows, cuts)
        return [[cell.text for cell in row] for row in cells], irregular

    def _extract_prose(self, page: Page, number: int, table_boxes: list[BBox]) -> list[ProseBlock]:
        """Every word not inside a table, grouped into visual blocks.

        Three documents state a fact only in prose; losing this would lose
        real records, so the block text always travels in full. ``kind`` is
        advisory reporting metadata and filters nothing.
        """
        words = [
            Word(
                text=str(word["text"]),
                x0=float(word["x0"]),
                x1=float(word["x1"]),
                top=float(word["top"]),
                bottom=float(word["bottom"]),
                confidence=ONE,
            )
            for word in page.extract_words()
            if not any(_overlap_fraction(word, box) > _IN_TABLE_OVERLAP for box in table_boxes)
        ]
        blocks: list[ProseBlock] = []
        for lines in group_lines_into_blocks(rows_to_lines(group_rows(words))):
            text = "\n".join(line.text for line in lines)
            if not text.strip():
                continue
            blocks.append(
                ProseBlock(
                    page_number=number,
                    kind=classify_block(text),
                    text=text,
                    bbox=(
                        min(line.x0 for line in lines),
                        min(line.top for line in lines),
                        max(line.x1 for line in lines),
                        max(line.bottom for line in lines),
                    ),
                    confidence=ONE,
                )
            )
        return blocks


def _normalize(value: str | None) -> str | None:
    """Collapse a cell's visual line wraps; keep everything else verbatim.

    ``None`` (merged-cell continuation) passes through untouched — it is the
    only signal that doc 01's "Taxable Income Bracket" header spans four
    columns.
    """
    if value is None:
        return None
    return " ".join(value.split())


def _looks_degenerate(grid: list[list[str | None]]) -> bool:
    """True when a one-column grid is really a flattened borderless table."""
    if not grid or any(len(row) != 1 for row in grid):
        return False
    populated = [row[0] for row in grid if row[0] is not None and row[0].strip()]
    if not populated:
        return False
    multi = sum(1 for text in populated if len(text.split()) >= _DEGENERATE_MIN_TOKENS)
    return multi >= len(populated) * _DEGENERATE_ROW_FRACTION


def _bbox(raw: Sequence[float]) -> BBox:
    x0, top, x1, bottom = (float(v) for v in raw)
    return x0, top, x1, bottom


def _overlap_fraction(word: dict[str, Any], box: BBox) -> float:
    """Fraction of a word's area that falls inside ``box``."""
    x0, top, x1, bottom = box
    dx = max(0.0, min(float(word["x1"]), x1) - max(float(word["x0"]), x0))
    dy = max(0.0, min(float(word["bottom"]), bottom) - max(float(word["top"]), top))
    area = (float(word["x1"]) - float(word["x0"])) * (float(word["bottom"]) - float(word["top"]))
    if area <= 0:
        return 0.0
    return dx * dy / area
