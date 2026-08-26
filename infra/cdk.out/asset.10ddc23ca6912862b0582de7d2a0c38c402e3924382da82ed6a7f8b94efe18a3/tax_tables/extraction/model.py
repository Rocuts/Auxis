"""The extracted-grid model: what a TableExtractor produces.

This is the only artifact the SchemaMapper will ever see (Phase 2b), so it
must carry everything the mapper needs to decide meaning:

- cell grids, verbatim — a dash stays a dash, an empty cell stays empty,
  a merged-cell continuation stays ``None`` (pdfplumber's colspan signal);
- every prose block and footnote on the page, because three documents state
  facts only outside their tables (doc 02's dependent rule, doc 03's rate
  unit, doc 05's surtax rate in a NOTE block);
- extraction-level confidence with provenance, so a doubtful cell can reach
  the review queue instead of being guessed (anti-goal #8);
- per-document extraction cost, because "4 of 5 documents cost $0 on every
  target" is a headline finding.

Confidence here is *extraction* confidence only (did we read the page
faithfully?). Mapping confidence (did we interpret it correctly?) belongs to
Phase 2b. Aggregates deliberately favor tail statistics over means: a mean
rewards an extractor that silently drops the hard table and averages the
easy prose — the exact failure mode anti-goal #8 forbids.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

ONE = Decimal(1)


class ExtractionMethod(StrEnum):
    """How a page's content was read."""

    DETERMINISTIC_TEXT = "deterministic_text"  # text layer, no AI service, $0
    OCR = "ocr"  # tesseract locally, Textract on AWS, vision-OCR on Vercel
    NONE = "none"  # blank page: nothing to extract, nothing spent


class GridSource(StrEnum):
    """Which mechanism produced a table's cell grid (provenance for debugging
    and for the extraction report)."""

    RULED_LINES = "ruled_lines"  # pdfplumber default lines strategy
    WORD_GAP_REBUILD = "word_gap_rebuild"  # columns re-inferred from word x-gaps
    RULED_CELL_OCR = "ruled_cell_ocr"  # image line grid + per-cell OCR


def percentile(values: list[Decimal], q: Decimal) -> Decimal:
    """Nearest-rank percentile; conservative (rounds the rank down)."""
    if not values:
        raise ValueError("percentile of empty list")
    ordered = sorted(values)
    rank = int(q * (len(ordered) - 1))
    return ordered[rank]


class Cell(BaseModel):
    """One table cell, verbatim.

    ``text`` semantics (faithful to pdfplumber): ``None`` means this position
    is spanned by a merged cell to its left/above (colspan continuation);
    ``""`` means the cell exists and is genuinely empty. OCR adapters must
    preserve the same distinction.

    ``ink_without_text`` is the OCR honesty flag: the cell region contains
    ink but the engine produced no text. Such a cell keeps ``text=""`` with
    ``confidence=0`` — visible data loss, never silent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str | None
    confidence: Decimal = Field(default=ONE, ge=Decimal(0), le=ONE)
    ink_without_text: bool = False


class ProseKind(StrEnum):
    HEADING = "heading"
    BODY = "body"
    FOOTNOTE = "footnote"


class ProseBlock(BaseModel):
    """Text outside any table, grouped into a visual block.

    Classification is a convenience heuristic for reporting; the full text
    always travels regardless of ``kind``, so a misclassified block loses
    nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    page_number: int = Field(ge=1)
    kind: ProseKind
    text: str = Field(min_length=1)
    bbox: tuple[float, float, float, float]  # (x0, top, x1, bottom), PDF points
    confidence: Decimal = Field(default=ONE, ge=Decimal(0), le=ONE)


class ExtractedTable(BaseModel):
    """One table's cell grid with quality accounting.

    ``rows`` may be ragged when a source row genuinely disagrees with the
    table's column count; such rows are listed in ``irregular_row_indexes``
    rather than force-fit into the grid (the arity assertion is the defense
    of the lightweight column-inference approach).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    page_number: int = Field(ge=1)
    table_id: str = Field(min_length=1)  # e.g. "p1_t0"; stable within a document
    bbox: tuple[float, float, float, float]
    grid_source: GridSource
    rows: list[list[Cell]]
    column_count: int = Field(ge=1)
    irregular_row_indexes: list[int] = Field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def cell_confidences(self) -> list[Decimal]:
        return [c.confidence for row in self.rows for c in row if c.text is not None]

    @property
    def flagged_cell_count(self) -> int:
        return sum(1 for row in self.rows for c in row if c.ink_without_text)

    @property
    def confidence(self) -> Decimal:
        """Tail-based aggregate, two guards deep.

        ``min(p10 of cell confidences, 1 - flagged fraction)`` scaled by the
        fraction of rows that fit the grid. The p10 tail catches diffuse
        low-quality cells a mean would launder; the flagged-fraction cap
        catches the opposite dodge, where a *few* wholly lost cells are too
        rare to move the tail — one unreadable cell in twenty must never
        report as 1.0. An empty grid is confidence 0.
        """
        confs = self.cell_confidences
        if not confs:
            return Decimal(0)
        tail = percentile(confs, Decimal("0.1"))
        readable = ONE - Decimal(self.flagged_cell_count) / Decimal(len(confs))
        regular = ONE - Decimal(len(self.irregular_row_indexes)) / Decimal(len(self.rows))
        return (min(tail, readable) * regular).quantize(Decimal("0.0001"))


class OcrPageStats(BaseModel):
    """Coverage + tail statistics for one OCR'd page. ``word_count`` guards
    against silent dropout: a run that finds far fewer words than the page
    plainly holds must fail loudly, not average well."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    word_count: int = Field(ge=0)
    mean_confidence: Decimal = Field(ge=Decimal(0), le=ONE)
    p10_confidence: Decimal = Field(ge=Decimal(0), le=ONE)
    low_confidence_fraction: Decimal = Field(ge=Decimal(0), le=ONE)  # conf < 0.8


class PageExtraction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    page_number: int = Field(ge=1)
    width: float
    height: float
    method: ExtractionMethod
    tables: list[ExtractedTable] = Field(default_factory=list)
    prose: list[ProseBlock] = Field(default_factory=list)
    ocr_stats: OcrPageStats | None = None


class ExtractionCost(BaseModel):
    """What extracting this document actually spent. Deterministic and local
    OCR paths are $0 by construction; only hosted AI services (Textract,
    vision-OCR) may report nonzero cost."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    engine: str = Field(min_length=1)
    api_calls: int = Field(default=0, ge=0)
    usd: Decimal = Field(default=Decimal(0), ge=Decimal(0))
    wall_seconds: float = Field(ge=0)


class ExtractedDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    filename: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pages: list[PageExtraction]
    cost: ExtractionCost

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def tables(self) -> list[ExtractedTable]:
        return [t for page in self.pages for t in page.tables]

    @property
    def prose(self) -> list[ProseBlock]:
        return [b for page in self.pages for b in page.prose]

    @property
    def methods(self) -> set[ExtractionMethod]:
        return {p.method for p in self.pages}

    @property
    def confidence(self) -> Decimal:
        """Conservative document aggregate: the minimum over table
        confidences and OCR page p10s. A document with no tables and no OCR
        stats has nothing to be wrong about yet: confidence 1."""
        floor = [t.confidence for t in self.tables]
        floor += [p.ocr_stats.p10_confidence for p in self.pages if p.ocr_stats is not None]
        return min(floor, default=ONE)
