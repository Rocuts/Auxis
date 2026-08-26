#!/usr/bin/env python3
"""Regenerate ``fixtures/textract/05_response.json``.

**HAND-CONSTRUCTED representative fixture.** CLAUDE.md sets the rule this
script exists to obey: *"Record one real Textract response for document 05
into fixtures/textract/05_response.json if credentials ever become
available; until then, hand-construct a representative fixture from the
documented BLOCK / CELL / RELATIONSHIPS shape and label it as such."* There
is no AWS account and no budget, so this is the hand-constructed half, and
the label travels inside the JSON as ``_provenance`` so nobody downstream
can mistake it for a recording.

Where the content comes from
----------------------------
The *shape* is Textract's documented block model. The *content* is document
05 itself: every string below was read off
``fixtures/05_capital_gains_preferential_rates_TY2025.pdf`` by running the
local OCR adapter (``TesseractExtractor``) over page 1 and transcribing what
it returned — grid, prose and all. The ground truth is the accuracy
harness's oracle and was not opened (anti-goal #1); nothing here is copied
from it, and nothing here is invented.

One correction and two deliberate liberties, stated rather than hidden:

1. The local engine reads the footnote's "Table 1" as "Table |" — a glyph
   confusion of one OCR engine. Modelling a *different* engine's response
   around another engine's specific misread would be its own kind of
   fiction, so the printed text is used.
2. Textract's TABLES feature routinely folds a caption band sitting directly
   above a bordered table into the table itself, as a merged first row. This
   fixture does that for Table 1 and leaves Table 2's caption outside as
   prose — engines are not consistent about it, and the adapter has to
   survive both. That makes Table 1 five rows here where the line-grid path
   reports four, and it is the merged-cell span the adapter's continuation
   handling is tested against. The caption text is printed on the page; no
   content is added or lost either way, it only moves between grid and prose.
3. ``Geometry.Polygon`` is omitted (the adapter reads only ``BoundingBox``)
   to keep a hand-written file reviewable. ``EntityTypes`` *is* kept on
   header cells even though the adapter ignores it — a faithful response
   carries it, and what a header means is the SchemaMapper's decision, not
   the extractor's.

Confidences are authored, not measured: mostly 90-97, with two words in one
cell down at 55.0 so the adapter's "minimum, not mean" cell rule and the
page's tail statistics both have something real to bite on.

Run ``uv run python fixtures/gen_textract_fixture.py`` to rewrite the file;
the output is byte-identical run to run, and a test asserts the committed
file matches it.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: (x0, top, x1, bottom) in PDF points, origin top-left — the frame the page
#: is authored in. Textract reports normalized [0, 1] geometry, so every box
#: is divided by the page size on the way out.
Box = tuple[float, float, float, float]

PAGE_WIDTH_PT = 612.0
PAGE_HEIGHT_PT = 792.0

OUT_PATH = Path(__file__).resolve().parent / "textract" / "05_response.json"
SOURCE_PDF = "fixtures/05_capital_gains_preferential_rates_TY2025.pdf"

PROVENANCE = (
    "HAND-CONSTRUCTED representative fixture (CLAUDE.md): shaped from the documented "
    "Textract BLOCK/CELL/RELATIONSHIPS model, content transcribed from "
    f"{SOURCE_PDF} via local OCR. NOT a recorded API response; replace with one if "
    "credentials ever become available."
)

#: Fixed namespace so block ids are uuid-shaped and identical on every run.
#: Random ids would make the file churn on regeneration and its diffs
#: unreadable.
_NAMESPACE = uuid.UUID("1b0e2d3c-4a59-4c6e-9f80-6a5b4c3d2e1f")

#: Word-level confidences cycle through this list. Plausible spread, no
#: randomness, and every value comfortably above the 0.8 low-confidence line
#: except where a run is overridden below.
_CONFIDENCE_CYCLE = (96.4, 94.8, 95.9, 93.2, 97.1, 92.6, 95.1, 96.8, 91.4, 94.2)

#: The scan's weakest cell. The local OCR reads this one at 0.60 while every
#: other cell lands at 0.92-0.96, so it is the honest place to put the tail:
#: two of its three words are authored at 55.0.
_LOW_CONFIDENCE_CELL = (3, 2)  # (grid row, column) in Table 1
_LOW_CONFIDENCE_WORDS = (95.1, 55.0, 55.0)

#: Layout constants for placing word boxes. A Times 8.2pt table glyph runs
#: about 4pt wide; body text on this page measures ~3.9pt/char.
_CHAR_WIDTH = 4.0
_TEXT_HEIGHT = 9.0
_CELL_PAD = 4.0

#: Table 1: five column edges plus the right border, and the row bands. The
#: first band is the caption row (see docstring); the rest are the header and
#: the three rate rows the local extraction reports.
_T1_COLUMNS = (52.08, 113.28, 221.28, 336.48, 451.68, 559.68)
_T1_ROWS = (
    (252.0, 272.4),
    (272.4, 296.0),
    (296.0, 320.0),
    (320.0, 343.6),
    (343.6, 367.2),
)
#: The caption line's own extent: it starts inside column 1 and runs into
#: column 3, which is what makes it a three-column merge.
_T1_CAPTION_BOX: Box = (74.4, 255.0, 334.0, 265.0)
_T1_CAPTION = "Table 1. Preferential rate bands by filing status, taxable income"
_T1_BODY: tuple[tuple[str, ...], ...] = (
    ("Rate", "Single", "Married Filing Jointly", "Married Filing Separately", "Head of Household"),
    ("0 percent", "$0 to $48,350", "$0 to $96,700", "$0 to $48,350", "$0 to $64,750"),
    (
        "15 percent",
        "$48,351 to $533,400",
        "$96,701 to $600,050",
        "$48,351 to $300,000",
        "$64,751 to $566,700",
    ),
    ("20 percent", "Over $533,400", "Over $600,050", "Over $300,000", "Over $566,700"),
)

#: Table 2: two columns, four rows, no merges. Its first column is
#: left-aligned on the page; the second is centred.
_T2_COLUMNS = (118.56, 377.76, 492.96)
_T2_ROWS = (
    (396.48, 420.18),
    (420.18, 443.88),
    (443.88, 467.58),
    (467.58, 491.28),
)
_T2_BODY: tuple[tuple[str, str], ...] = (
    ("Category", "Maximum rate"),
    ("Unrecaptured section 1250 gain", "25 percent"),
    ("Collectibles and certain small business stock", "28 percent"),
    ("Short-term capital gain", "Ordinary rates"),
)

#: Every line of printed text outside the two tables, in page order, with the
#: box it occupies. Transcribed from the local OCR pass; the boxes follow the
#: block bounds that pass reported.
_PROSE_LINES: tuple[tuple[str, Box], ...] = (
    ("INTERNAL CIRCULAR — CAPITAL GAINS DIVISION", (216.0, 79.44, 396.0, 88.0)),
    (
        "Preferential Rates on Long-Term Capital Gain and Qualified Dividend",
        (94.32, 100.0, 517.68, 112.0),
    ),
    ("Income", (287.0, 112.5, 325.0, 124.5)),
    ("Tax Year 2025 — Circular CG-2025/07", (225.5, 130.0, 386.5, 141.36)),
    (
        "SUPERSEDED. This circular states the rates applicable to taxable years "
        "beginning before January 1, 2026. It is retained",
        (74.64, 160.32, 537.84, 170.5),
    ),
    (
        "for reference in connection with amended returns and prior-period assessments. "
        "For taxable years beginning on or after",
        (74.64, 173.3, 537.84, 183.5),
    ),
    ("January 1, 2026, see Circular CG-2026/03.", (74.64, 186.3, 232.0, 196.5)),
    (
        "Net long-term capital gain and qualified dividend income are taxed at the "
        "preferential rates set out below. The applicable",
        (74.64, 204.0, 537.84, 214.2),
    ),
    (
        "rate is determined by reference to the taxpayer's total taxable income, "
        "including the gain itself. Where taxable income spans",
        (74.64, 217.0, 537.84, 227.2),
    ),
    (
        "more than one band, the gain is allocated across the bands in order and each "
        "portion is taxed at the corresponding rate.",
        (74.64, 230.0, 537.84, 240.2),
    ),
    ("Table 2. Special rate categories", (74.4, 380.64, 200.16, 389.28)),
    (
        "NOTE. An additional Net Investment Income Tax of 3.8 percent applies to the "
        "lesser of net investment income or the excess of modified",
        (74.4, 502.32, 530.88, 511.0),
    ),
    (
        "adjusted gross income over $200,000 (single and head of household), $250,000 "
        "(married filing jointly) or $125,000 (married filing separately).",
        (74.4, 512.5, 530.88, 521.2),
    ),
    (
        "This surtax is imposed in addition to the rates shown in Table 1 and is not "
        "reflected in those rates.",
        (74.4, 523.0, 414.0, 531.84),
    ),
    (
        "Synthetic sample data for systems evaluation. Not an authoritative tax publication.",
        (68.16, 751.2, 352.0, 757.68),
    ),
    ("CG-2025/07 — 1", (480.0, 751.2, 542.16, 757.68)),
)


@dataclass(frozen=True)
class _Word:
    """A WORD block, kept around so cells and lines can share the same ids —
    which is what the real API does: a word belongs to a LINE *and* to the
    CELL it falls in."""

    id: str
    text: str
    box: Box
    confidence: float


def _block_id(label: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, label))


def _geometry(box: Box) -> dict[str, Any]:
    x0, top, x1, bottom = box
    return {
        "BoundingBox": {
            "Width": round((x1 - x0) / PAGE_WIDTH_PT, 6),
            "Height": round((bottom - top) / PAGE_HEIGHT_PT, 6),
            "Left": round(x0 / PAGE_WIDTH_PT, 6),
            "Top": round(top / PAGE_HEIGHT_PT, 6),
        }
    }


def _tile(text: str, box: Box) -> list[tuple[str, Box]]:
    """Lay the words of ``text`` across ``box``, widths proportional to
    length. Exact tiling keeps a LINE's geometry consistent with the WORDs it
    contains, which is the invariant a reader will spot-check first."""
    words = text.split()
    x0, top, x1, bottom = box
    units = sum(len(word) for word in words) + max(len(words) - 1, 0)
    step = (x1 - x0) / units
    placed: list[tuple[str, Box]] = []
    cursor = x0
    for index, word in enumerate(words):
        width = len(word) * step
        placed.append((word, (round(cursor, 2), top, round(cursor + width, 2), bottom)))
        cursor += width + (step if index < len(words) - 1 else 0.0)
    return placed


def _text_box(text: str, cell: Box, *, align: str = "center") -> Box:
    """Where a cell's text actually sits inside the cell rectangle."""
    x0, top, x1, bottom = cell
    width = min(len(text) * _CHAR_WIDTH, (x1 - x0) - 2 * _CELL_PAD)
    left = x0 + _CELL_PAD if align == "left" else (x0 + x1) / 2 - width / 2
    middle = (top + bottom) / 2
    return (
        round(left, 2),
        middle - _TEXT_HEIGHT / 2,
        round(left + width, 2),
        middle + _TEXT_HEIGHT / 2,
    )


class _Builder:
    """Accumulates blocks. Text blocks (LINE + its WORDs) are grouped so the
    finished response can be emitted in reading order the way a real one is,
    while the adapter is free to ignore order entirely and resolve by id."""

    def __init__(self) -> None:
        self.text_groups: list[tuple[Box, list[dict[str, Any]]]] = []
        self.table_blocks: list[dict[str, Any]] = []
        self.line_ids: list[str] = []
        self.table_ids: list[str] = []
        self._pending: list[dict[str, Any]] = []
        self._ordinal = 0

    def _confidence(self) -> float:
        value = _CONFIDENCE_CYCLE[self._ordinal % len(_CONFIDENCE_CYCLE)]
        self._ordinal += 1
        return value

    def line(
        self, label: str, text: str, box: Box, confidences: tuple[float, ...] | None = None
    ) -> list[_Word]:
        """Emit one LINE block and the WORD blocks under it."""
        words: list[_Word] = []
        for index, (token, word_box) in enumerate(_tile(text, box)):
            confidence = (
                confidences[index]
                if confidences is not None and index < len(confidences)
                else self._confidence()
            )
            words.append(
                _Word(
                    id=_block_id(f"{label}-word-{index}"),
                    text=token,
                    box=word_box,
                    confidence=confidence,
                )
            )
        line_id = _block_id(f"{label}-line")
        blocks: list[dict[str, Any]] = [
            {
                "BlockType": "LINE",
                "Confidence": round(min(word.confidence for word in words), 2),
                "Text": text,
                "Geometry": _geometry(box),
                "Id": line_id,
                "Relationships": [{"Type": "CHILD", "Ids": [word.id for word in words]}],
            }
        ]
        blocks.extend(
            {
                "BlockType": "WORD",
                "Confidence": word.confidence,
                "Text": word.text,
                "TextType": "PRINTED",
                "Geometry": _geometry(word.box),
                "Id": word.id,
            }
            for word in words
        )
        self.text_groups.append((box, blocks))
        self.line_ids.append(line_id)
        return words

    def cell(
        self,
        label: str,
        *,
        row: int,
        column: int,
        box: Box,
        words: list[_Word],
        header: bool = False,
    ) -> str:
        block: dict[str, Any] = {
            "BlockType": "CELL",
            "Confidence": 98.0,
            "RowIndex": row,
            "ColumnIndex": column,
            "RowSpan": 1,
            "ColumnSpan": 1,
            "Geometry": _geometry(box),
            "Id": _block_id(label),
        }
        if header:
            # Textract labels header cells; the adapter deliberately ignores
            # the hint. Deciding that a row is a header is the SchemaMapper's
            # job, on the whole grid, not the extractor's on one block.
            block["EntityTypes"] = ["COLUMN_HEADER"]
        if words:
            block["Relationships"] = [{"Type": "CHILD", "Ids": [word.id for word in words]}]
        self._pending.append(block)
        return str(block["Id"])

    def merged_cell(
        self,
        label: str,
        *,
        row: int,
        column: int,
        row_span: int,
        column_span: int,
        box: Box,
        cell_ids: list[str],
    ) -> str:
        block: dict[str, Any] = {
            "BlockType": "MERGED_CELL",
            "Confidence": 96.0,
            "RowIndex": row,
            "ColumnIndex": column,
            "RowSpan": row_span,
            "ColumnSpan": column_span,
            "Geometry": _geometry(box),
            "Id": _block_id(label),
            # A MERGED_CELL's children are the CELLs it subsumes, not words.
            "Relationships": [{"Type": "CHILD", "Ids": cell_ids}],
        }
        self._pending.append(block)
        return str(block["Id"])

    def table(self, label: str, *, box: Box, child_ids: list[str]) -> None:
        """Close a table: its TABLE block, then the cells built since the last
        one. Table order in the response is page order, and the adapter reads
        it as such when it numbers ``p1_t0``, ``p1_t1``."""
        table_id = _block_id(label)
        self.table_blocks.append(
            {
                "BlockType": "TABLE",
                "Confidence": 99.0,
                "Geometry": _geometry(box),
                "Id": table_id,
                "Relationships": [{"Type": "CHILD", "Ids": child_ids}],
            }
        )
        self.table_blocks.extend(self._pending)
        self._pending = []
        self.table_ids.append(table_id)


def _cell_box(
    columns: tuple[float, ...], rows: tuple[tuple[float, float], ...], row: int, column: int
) -> Box:
    top, bottom = rows[row - 1]
    return (columns[column - 1], top, columns[column], bottom)


def _build_table_one(builder: _Builder) -> None:
    columns, rows = _T1_COLUMNS, _T1_ROWS
    cell_ids: list[str] = []

    # Row 1 — the caption band Textract folded into the table. One visual
    # line, so one LINE block; its words fall into the first three columns,
    # which is exactly what makes those three cells a merge.
    caption_words = builder.line("t1-caption", _T1_CAPTION, _T1_CAPTION_BOX)
    merged_columns = 3
    per_column: list[list[_Word]] = [[] for _ in range(merged_columns)]
    for word in caption_words:
        centre = (word.box[0] + word.box[2]) / 2
        for column in range(1, merged_columns + 1):
            if columns[column - 1] <= centre < columns[column]:
                per_column[column - 1].append(word)
                break

    caption_cell_ids: list[str] = []
    for column in range(1, len(columns)):
        words = per_column[column - 1] if column <= merged_columns else []
        cell_id = builder.cell(
            f"t1-r1c{column}",
            row=1,
            column=column,
            box=_cell_box(columns, rows, 1, column),
            words=words,
        )
        cell_ids.append(cell_id)
        if column <= merged_columns:
            caption_cell_ids.append(cell_id)

    merged_box: Box = (columns[0], rows[0][0], columns[merged_columns], rows[0][1])
    cell_ids.append(
        builder.merged_cell(
            "t1-merged-caption",
            row=1,
            column=1,
            row_span=1,
            column_span=merged_columns,
            box=merged_box,
            cell_ids=caption_cell_ids,
        )
    )

    # Rows 2-5 — the header and the three rate rows, as printed.
    for offset, texts in enumerate(_T1_BODY):
        row = offset + 2
        for column, text in enumerate(texts, start=1):
            box = _cell_box(columns, rows, row, column)
            label = f"t1-r{row}c{column}"
            confidences = _LOW_CONFIDENCE_WORDS if (row, column) == _LOW_CONFIDENCE_CELL else None
            words = builder.line(label, text, _text_box(text, box), confidences)
            cell_ids.append(
                builder.cell(label, row=row, column=column, box=box, words=words, header=row == 2)
            )

    builder.table(
        "t1",
        box=(columns[0], rows[0][0], columns[-1], rows[-1][1]),
        child_ids=cell_ids,
    )


def _build_table_two(builder: _Builder) -> None:
    columns, rows = _T2_COLUMNS, _T2_ROWS
    cell_ids: list[str] = []
    for offset, texts in enumerate(_T2_BODY):
        row = offset + 1
        for column, text in enumerate(texts, start=1):
            box = _cell_box(columns, rows, row, column)
            label = f"t2-r{row}c{column}"
            align = "left" if column == 1 and row > 1 else "center"
            words = builder.line(label, text, _text_box(text, box, align=align))
            cell_ids.append(
                builder.cell(label, row=row, column=column, box=box, words=words, header=row == 1)
            )
    builder.table(
        "t2",
        box=(columns[0], rows[0][0], columns[-1], rows[-1][1]),
        child_ids=cell_ids,
    )


def build() -> dict[str, Any]:
    """Assemble the whole AnalyzeDocument response."""
    builder = _Builder()
    for index, (text, box) in enumerate(_PROSE_LINES):
        builder.line(f"prose-{index}", text, box)
    _build_table_one(builder)
    _build_table_two(builder)

    # Reading order for the text blocks, as a real response emits them; the
    # PAGE block owns every line and every table.
    text_blocks: list[dict[str, Any]] = []
    for _, blocks in sorted(builder.text_groups, key=lambda item: (item[0][1], item[0][0])):
        text_blocks.extend(blocks)

    page: dict[str, Any] = {
        "BlockType": "PAGE",
        "Geometry": _geometry((0.0, 0.0, PAGE_WIDTH_PT, PAGE_HEIGHT_PT)),
        "Id": _block_id("page-1"),
        "Relationships": [{"Type": "CHILD", "Ids": builder.line_ids + builder.table_ids}],
    }
    return {
        "DocumentMetadata": {"Pages": 1},
        "Blocks": [page, *text_blocks, *builder.table_blocks],
        "AnalyzeDocumentModelVersion": "1.0",
        "_provenance": PROVENANCE,
    }


def render() -> str:
    """The exact file text, deterministic across runs."""
    return json.dumps(build(), indent=2) + "\n"


def write(path: Path = OUT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(), encoding="utf-8")
    return path


if __name__ == "__main__":
    written = write()
    print(f"wrote {written} ({written.stat().st_size} bytes) - {PROVENANCE.split(':')[0]}")
