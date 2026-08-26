"""AWS OCR TableExtractor: Textract ``AnalyzeDocument`` with ``FeatureTypes=["TABLES"]``.

This adapter serves the *AWS* target only. It never runs on Vercel (the
vision-OCR adapter is that target's pixel-licensed port) and never locally
(tesseract is). Like every other extractor it is licensed to read pixels and
nothing downstream is: the SchemaMapper only ever sees the cell grid this
module produces.

Why one call per page
---------------------
The synchronous ``AnalyzeDocument`` API takes a *single-page* image or PDF,
so a page is the unit of work — and therefore the unit of cost, of latency
and of parallelism. The router decides which pages arrive here at all, which
is what keeps the corpus's headline true: four of five documents have a text
layer, reach pdfplumber instead, and cost $0 on every target. Only fixture
05 ever reaches this module, and its bill is one page.

Pages are rendered with pypdfium2 at the same 300 DPI the local OCR adapter
measured as the best trade on this corpus (150 DPI breaks the strokes of a
~200 DPI scan; 400+ magnifies its JPEG artifacts). Textract's own guidance
is a 150 DPI floor, so 300 clears it with margin. The render is RGB PNG: the
sibling adapter measured grayscale actively misreading '15 percent' as
'|S percent', and there is no reason to hand a hosted engine a worse image
than a local one.

Fidelity rules, each mapped to a documented BLOCK-model detail:

- A CELL's text is its WORD children joined by single spaces, and its
  confidence is the **minimum** of those words' confidences, not the mean. A
  mean rewards dropout: one word read at 55 among nine read at 96 averages
  to 91.9 and sails past every threshold, while the minimum puts the cell in
  front of a human (anti-goal #8). A cell with no words has nothing to
  misread and is confidently empty.
- A MERGED_CELL keeps its text at the anchor position (top-left of the span)
  and every other position it covers becomes ``Cell(text=None)`` — the same
  merged-continuation convention the pdfplumber adapter emits and the
  mapper's serializer already reads. Plain CELLs carrying RowSpan or
  ColumnSpan greater than one are treated identically.
- A grid position that no CELL block claims at all can only be the
  continuation of a span, so the grid starts as continuation and each cell
  writes itself in.
- LINE blocks whose centre falls outside every TABLE bbox are prose. Losing
  them loses records: fixture 05's 3.8 percent surtax rate exists *only* in
  its NOTE block, and its 'Tax Year 2025' sentence is the only lawful source
  for that document's tax year.
- A malformed block is a fault, not a shrug. A CELL pointing at a WORD id
  the response does not contain raises ``ValueError`` naming the offending
  block rather than yielding a quietly shorter table.

Geometry is reported in the pixels of the image Textract was actually shown
— the frame its normalized coordinates are defined against — and
``PageExtraction.width``/``height`` are that image's dimensions, so bboxes
and page size share one unit. (The local adapter reports PDF points instead;
multiply by ``page_width_pt / image_width`` here for parity if the pipeline
ever needs one frame across engines.)

Cost is tracked per page like every other engine: list price for
``AnalyzeDocument`` with the TABLES feature, overridable through
``TEXTRACT_USD_PER_PAGE`` so a negotiated rate or a price change is a config
edit rather than a code change.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pypdfium2 as pdfium

from tax_tables.extraction.model import (
    ONE,
    Cell,
    ExtractedTable,
    ExtractionMethod,
    GridSource,
    OcrPageStats,
    PageExtraction,
    ProseBlock,
    percentile,
)
from tax_tables.extraction.prose import classify_block
from tax_tables.extraction.render import render_page_png
from tax_tables.ports.extractor import PageBatch

#: One block of a Textract response. Deliberately a ``Mapping`` rather than a
#: TypedDict: the response is JSON from a service that adds fields over time,
#: and this module reads the documented subset it needs.
Block = Mapping[str, Any]
BBox = tuple[float, float, float, float]

#: The synchronous API caps inline document bytes at 10 MB. A page over the
#: cap fails here, naming the page, rather than as an opaque ClientError from
#: the SDK — and never by silently downscaling the evidence.
_MAX_SYNC_BYTES = 10 * 1024 * 1024

#: List price per page for AnalyzeDocument with the TABLES feature (first
#: million pages/month, us-east-1). It feeds the per-document cost accounting
#: the same way every other engine's does; the local and deterministic paths
#: report $0 there by construction.
_DEFAULT_USD_PER_PAGE = Decimal("0.015")
_PRICE_ENV = "TEXTRACT_USD_PER_PAGE"

_HUNDRED = Decimal(100)
_QUANTUM = Decimal("0.0001")
_LOW_CONFIDENCE = Decimal("0.8")

#: A line further below its predecessor than this multiple of the
#: predecessor's height starts a new prose block. Same rule, same constant as
#: the digital and local adapters' block grouping.
_BLOCK_GAP_FACTOR = 1.5


@dataclass(frozen=True)
class _Placement:
    """Where a CELL or MERGED_CELL sits in the grid. Textract indexes rows and
    columns from 1; spans default to 1."""

    row: int
    column: int
    row_span: int
    column_span: int


@dataclass(frozen=True)
class _Line:
    """One LINE block reduced to what prose grouping needs."""

    text: str
    bbox: BBox
    confidence: Decimal

    @property
    def top(self) -> float:
        return self.bbox[1]

    @property
    def bottom(self) -> float:
        return self.bbox[3]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]


class TextractExtractor:
    """TableExtractor over Amazon Textract's synchronous AnalyzeDocument.

    The client is injected so the adapter is unit-testable with no
    credentials, no network and no boto3 — anything exposing
    ``analyze_document(Document=..., FeatureTypes=...)`` will do, which is
    how ``fixtures/textract/05_response.json`` is exercised.
    """

    def __init__(self, *, client: Any | None = None, usd_per_page: Decimal | None = None) -> None:
        self._client = client
        self._usd_per_page = _price_per_page() if usd_per_page is None else usd_per_page

    @property
    def engine(self) -> str:
        return "textract"

    def extract_pages(self, pdf_bytes: bytes, page_numbers: Sequence[int]) -> PageBatch:
        client = self._analyzer()
        pages: list[PageExtraction] = []
        pdf = pdfium.PdfDocument(pdf_bytes)
        try:
            for number in page_numbers:
                image, width, height = render_page_png(
                    pdf,
                    number,
                    max_bytes=_MAX_SYNC_BYTES,
                    limit_label="synchronous AnalyzeDocument",
                )
                response = client.analyze_document(
                    Document={"Bytes": image}, FeatureTypes=["TABLES"]
                )
                pages.append(_parse_page(response, page_number=number, width=width, height=height))
        finally:
            pdf.close()
        # One call per page, billed per page: the cost line is arithmetic, not
        # an estimate, and it is the number the README's cost table quotes.
        return PageBatch(pages=pages, api_calls=len(pages), usd=self._usd_per_page * len(pages))

    def _analyzer(self) -> Any:
        if self._client is None:
            self._client = _build_client()
        return self._client


def _price_per_page() -> Decimal:
    """Per-page price from the environment, falling back to list price.

    An unparseable override raises rather than quietly reverting to the
    default: a wrong cost report is worse than a loud one.
    """
    override = os.environ.get(_PRICE_ENV)
    return _DEFAULT_USD_PER_PAGE if override is None else Decimal(override)


def _build_client() -> Any:
    """Build a boto3 Textract client on first use.

    Imported here, by name, rather than at module scope: boto3 lives behind
    the ``aws`` extra because it must never ride the Vercel bundle, and this
    module has to import cleanly on a keyless dev machine that has never
    installed it — every test in this package runs against a recorded
    response with no SDK present.
    """
    try:
        boto3 = importlib.import_module("boto3")
    except ImportError as exc:  # pragma: no cover - exercised by env, not tests
        raise RuntimeError(
            "TextractExtractor needs boto3 to reach the Textract API. boto3 ships in the "
            "Lambda runtime and the `aws` extra; install with `uv sync --extra aws`. No "
            "SDK is needed to unit-test this adapter: inject a client that replays "
            "fixtures/textract/05_response.json."
        ) from exc
    return boto3.client("textract")


# --------------------------------------------------------------------------
# Response walking. Every accessor names the offending block when it fails:
# a bad response must be diagnosable from the exception alone.
# --------------------------------------------------------------------------


def _describe(block: Block) -> str:
    return f"{block.get('BlockType')} {block.get('Id')!r}"


def _blocks(response: Block) -> list[Block]:
    blocks = response.get("Blocks")
    if not isinstance(blocks, list):
        raise ValueError("Textract response carries no Blocks array")
    return list(blocks)


def _index(blocks: Sequence[Block]) -> dict[str, Block]:
    index: dict[str, Block] = {}
    for block in blocks:
        identifier = block.get("Id")
        if not isinstance(identifier, str):
            raise ValueError(f"Textract block without an Id: {block.get('BlockType')!r}")
        index[identifier] = block
    return index


def _children(block: Block, index: Mapping[str, Block]) -> list[Block]:
    """CHILD-relationship blocks, resolved by id.

    An id the response does not contain is a fault: resolving it to nothing
    would drop a word, a cell or a whole row without a trace.
    """
    children: list[Block] = []
    for relationship in block.get("Relationships") or ():
        if relationship.get("Type") != "CHILD":
            continue
        for child_id in relationship.get("Ids") or ():
            child = index.get(child_id)
            if child is None:
                raise ValueError(
                    f"Textract block {_describe(block)} references unknown child {child_id!r}"
                )
            children.append(child)
    return children


def _words(block: Block, index: Mapping[str, Block]) -> list[Block]:
    """WORD descendants, in relationship order.

    One level of indirection is documented and load-bearing: a MERGED_CELL's
    children are the CELLs it merges, and those CELLs hold the WORDs. Walking
    through them is what gives a merged cell its full text.
    """
    words: list[Block] = []
    for child in _children(block, index):
        kind = child.get("BlockType")
        if kind == "WORD":
            words.append(child)
        elif kind == "CELL":
            words.extend(_words(child, index))
    return words


def _word_text(word: Block) -> str:
    text = word.get("Text")
    if not isinstance(text, str):
        raise ValueError(f"Textract {_describe(word)} carries no Text")
    return text


def _confidence(block: Block) -> Decimal:
    raw = block.get("Confidence")
    if not isinstance(raw, int | float):
        raise ValueError(f"Textract {_describe(block)} carries no Confidence")
    scaled = Decimal(str(raw)) / _HUNDRED
    return min(max(scaled, Decimal(0)), ONE).quantize(_QUANTUM)


def _text_of(words: Sequence[Block]) -> str:
    return " ".join(_word_text(word) for word in words)


def _min_confidence(words: Sequence[Block]) -> Decimal:
    """Minimum, never mean — see the module docstring. No words means nothing
    was read wrong, which is a different claim from reading it badly."""
    return min((_confidence(word) for word in words), default=ONE)


def _bbox(block: Block, width: int, height: int) -> BBox:
    """Geometry.BoundingBox, normalized [0, 1], scaled to render pixels."""
    geometry = block.get("Geometry")
    box = geometry.get("BoundingBox") if isinstance(geometry, Mapping) else None
    if not isinstance(box, Mapping):
        raise ValueError(f"Textract {_describe(block)} carries no Geometry.BoundingBox")
    try:
        left = float(box["Left"]) * width
        top = float(box["Top"]) * height
        return (left, top, left + float(box["Width"]) * width, top + float(box["Height"]) * height)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Textract {_describe(block)} has an unreadable BoundingBox") from exc


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------


def _placement(block: Block) -> _Placement:
    try:
        row = int(block["RowIndex"])
        column = int(block["ColumnIndex"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Textract {_describe(block)} has no usable Row/ColumnIndex") from exc
    if row < 1 or column < 1:
        raise ValueError(f"Textract {_describe(block)} indexes from below 1: ({row}, {column})")
    return _Placement(
        row=row,
        column=column,
        row_span=max(int(block.get("RowSpan") or 1), 1),
        column_span=max(int(block.get("ColumnSpan") or 1), 1),
    )


def _cell(block: Block, index: Mapping[str, Block]) -> Cell:
    words = _words(block, index)
    if not words:
        # Textract reports no ink coverage, so the OCR honesty flag
        # ``ink_without_text`` has no evidence to stand on here and must not
        # be invented. An empty cell is an empty cell.
        return Cell(text="")
    return Cell(text=_text_of(words), confidence=_min_confidence(words))


def _place(grid: list[list[Cell]], placement: _Placement, cell: Cell) -> None:
    """Write a cell at its anchor and blank every position its span covers."""
    for row in range(placement.row - 1, placement.row + placement.row_span - 1):
        for column in range(placement.column - 1, placement.column + placement.column_span - 1):
            grid[row][column] = Cell(text=None)
    grid[placement.row - 1][placement.column - 1] = cell


def _table(
    block: Block,
    index: Mapping[str, Block],
    *,
    page_number: int,
    table_index: int,
    width: int,
    height: int,
) -> ExtractedTable:
    children = _children(block, index)
    cells = [child for child in children if child.get("BlockType") == "CELL"]
    merged = [child for child in children if child.get("BlockType") == "MERGED_CELL"]
    if not cells:
        raise ValueError(f"Textract {_describe(block)} has no CELL children")

    placed = [(child, _placement(child)) for child in cells]
    rows = max(place.row + place.row_span - 1 for _, place in placed)
    columns = max(place.column + place.column_span - 1 for _, place in placed)
    grid: list[list[Cell]] = [[Cell(text=None) for _ in range(columns)] for _ in range(rows)]

    for child, place in placed:
        _place(grid, place, _cell(child, index))
    # Merged cells last: they overwrite the individual cells they subsume, so
    # the anchor carries the whole merged text and the rest reads as
    # continuation.
    for child in merged:
        _place(grid, _placement(child), _cell(child, index))

    return ExtractedTable(
        page_number=page_number,
        table_id=f"p{page_number}_t{table_index}",
        bbox=_bbox(block, width, height),
        # No enum member names a hosted OCR service, and inventing one would
        # overstate the difference: Textract returns a ruled cell grid read
        # off pixels, which is exactly what the local per-cell OCR path
        # produces. Same provenance claim, different engine.
        grid_source=GridSource.RULED_CELL_OCR,
        rows=grid,
        column_count=columns,
    )


# --------------------------------------------------------------------------
# Prose
# --------------------------------------------------------------------------


def _inside(bbox: BBox, container: BBox) -> bool:
    """Is the centre of ``bbox`` inside ``container``?

    Centre containment, not overlap: a line brushing a table's border stays
    prose, and a line the table genuinely holds is never duplicated into
    both.
    """
    x = (bbox[0] + bbox[2]) / 2
    y = (bbox[1] + bbox[3]) / 2
    return container[0] <= x <= container[2] and container[1] <= y <= container[3]


def _line(block: Block, index: Mapping[str, Block], width: int, height: int) -> _Line:
    words = _words(block, index)
    if not words:
        raise ValueError(f"Textract {_describe(block)} has no WORD children")
    return _Line(
        text=_text_of(words),
        bbox=_bbox(block, width, height),
        confidence=_min_confidence(words),
    )


def _group(lines: Sequence[_Line]) -> list[list[_Line]]:
    """Group lines top-to-bottom; a gap over 1.5x the previous line's height
    starts a new block."""
    blocks: list[list[_Line]] = []
    for line in sorted(lines, key=lambda item: (item.top, item.bbox[0])):
        if blocks:
            previous = blocks[-1][-1]
            if line.top - previous.bottom <= _BLOCK_GAP_FACTOR * previous.height:
                blocks[-1].append(line)
                continue
        blocks.append([line])
    return blocks


def _prose(lines: Sequence[_Line], page_number: int) -> list[ProseBlock]:
    blocks: list[ProseBlock] = []
    for group in _group(lines):
        text = "\n".join(line.text for line in group)
        if not text.strip():
            continue
        blocks.append(
            ProseBlock(
                page_number=page_number,
                kind=classify_block(text),
                text=text,
                bbox=(
                    min(line.bbox[0] for line in group),
                    min(line.bbox[1] for line in group),
                    max(line.bbox[2] for line in group),
                    max(line.bbox[3] for line in group),
                ),
                confidence=min(line.confidence for line in group),
            )
        )
    return blocks


# --------------------------------------------------------------------------
# Page assembly
# --------------------------------------------------------------------------


def _page_stats(words: Sequence[Block]) -> OcrPageStats:
    """Coverage and tail statistics over every WORD the response carries —
    table words and prose words alike.

    ``word_count`` is the dropout guard: a response that reports far fewer
    words than the page plainly holds must look wrong in the extraction
    report rather than average well (anti-goal #8).
    """
    if not words:
        return OcrPageStats(
            word_count=0,
            mean_confidence=Decimal(0),
            p10_confidence=Decimal(0),
            low_confidence_fraction=ONE,
        )
    confidences = [_confidence(word) for word in words]
    total = sum(confidences, start=Decimal(0))
    low = sum(1 for confidence in confidences if confidence < _LOW_CONFIDENCE)
    count = Decimal(len(confidences))
    return OcrPageStats(
        word_count=len(confidences),
        mean_confidence=(total / count).quantize(_QUANTUM),
        p10_confidence=percentile(confidences, Decimal("0.1")),
        low_confidence_fraction=(Decimal(low) / count).quantize(_QUANTUM),
    )


def _parse_page(response: Block, *, page_number: int, width: int, height: int) -> PageExtraction:
    """Turn one AnalyzeDocument response into one PageExtraction.

    ``page_number`` is the page the *router* asked for, not the response's
    own — every synchronous call carries a single page and would always
    report 1.
    """
    blocks = _blocks(response)
    index = _index(blocks)

    tables = [
        _table(
            block,
            index,
            page_number=page_number,
            table_index=order,
            width=width,
            height=height,
        )
        for order, block in enumerate(b for b in blocks if b.get("BlockType") == "TABLE")
    ]
    table_boxes = [table.bbox for table in tables]

    lines = [
        _line(block, index, width, height) for block in blocks if block.get("BlockType") == "LINE"
    ]
    outside = [line for line in lines if not any(_inside(line.bbox, box) for box in table_boxes)]

    return PageExtraction(
        page_number=page_number,
        width=float(width),
        height=float(height),
        method=ExtractionMethod.OCR,
        tables=tables,
        prose=_prose(outside, page_number),
        ocr_stats=_page_stats([b for b in blocks if b.get("BlockType") == "WORD"]),
    )
