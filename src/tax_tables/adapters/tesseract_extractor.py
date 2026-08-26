"""Local OCR TableExtractor: tesseract over a pypdfium2 render.

This adapter serves the *local* target only (docker-compose and the test
suite). It never runs on Vercel — no system binaries there, which is why the
vision-OCR adapter exists — and never on AWS, where Textract is the OCR port.
It costs $0 and makes zero API calls: a local binary is not a hosted service.

Why per-cell OCR rather than whole-page OCR
-------------------------------------------
Measured against fixture 05 (a genuine ~200 DPI scan: skewed, blurred, noisy,
JPEG-compressed), *every* page-level segmentation mode loses table content:

* ``--psm 3`` drops the stub cells 'Rate', 'Single', '0 percent',
  '20 percent', '25 percent';
* ``--psm 4`` drops the whole of Table 1's body;
* ``--psm 11`` recovers words but destroys paragraph structure.

The ruling lines are what defeat page-level layout analysis. Detecting the
line grid first and OCR'ing each cell in isolation recovers all of those
cells at confidence 94-96. So: tables come from the line grid + per-cell OCR,
and prose comes from a page-level pass over an image with the table regions
whited out. There is deliberately no page-level fallback for tables — it is
the failure mode this design exists to avoid (anti-goal #8).

Pipeline, in order, with the parameters a prototype validated on fixture 05:

1. render at 300 DPI in RGB (150 DPI and 400+ both measured worse; rendering
   in grayscale measurably misreads '15 percent' as '|S percent', so the OCR
   input stays RGB and grayscale copies are made for *analysis* only);
2. deskew by an estimated angle — never a hardcoded one;
3. binarize by Otsu's threshold, computed from the histogram;
4. find horizontal rules, then vertical rules within each band;
5. assemble bands sharing a vertical-rule set into tables;
6. OCR each cell; a cell with ink but no text is flagged, never dropped;
7. OCR the page with tables whited out, and group the words into prose
   blocks — fixture 05's 3.8 percent surtax rate exists *only* in a NOTE
   block, so losing prose loses a record.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from statistics import fmean, pvariance
from typing import TYPE_CHECKING, Any, cast

import pypdfium2 as pdfium
from PIL import Image, ImageDraw

from tax_tables.extraction.gridbuild import Word, group_lines_into_blocks, group_rows, rows_to_lines
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
from tax_tables.ports.extractor import PageBatch

if TYPE_CHECKING:
    from types import ModuleType

#: Render resolution. 300 DPI measured better than both 150 and 400+ on the
#: ~200 DPI native scan: below it the strokes break up, above it the JPEG
#: artifacts are magnified along with the glyphs.
RENDER_DPI = 300
_PDF_POINTS_PER_INCH = 72

#: Deskew search. The scan sits at about -0.55 deg; a +-2 deg window at 0.1 deg
#: resolution covers any plausible feeder skew without inviting a false
#: maximum from page furniture.
_SKEW_LIMIT_TENTHS = 20
_MIN_CORRECTED_SKEW = 0.05  # below this, rotating costs resampling and buys nothing

#: A row is part of a horizontal rule when this fraction of it is ink.
_HRULE_INK_FRACTION = 0.30
#: A column is part of a vertical rule when this fraction of the band is ink.
_VRULE_INK_FRACTION = 0.55
#: Rule rows/columns this close together belong to the same rule.
_RULE_CLUSTER_GAP = 3
#: Real ruled lines on this corpus are 4-8px at 300 DPI.
_MAX_RULE_THICKNESS = 12
#: A ruled line is *continuous*; text is not. A row of bold capitals in
#: fixture 05's header band projects to an ink fraction of 0.26-0.35 — over
#: the threshold above, and only 2-6px thick, so neither the fraction nor the
#: thickness test rejects it. Its longest unbroken ink run is one word wide,
#: while the table's real rules run 1560-2115px. Requiring a contiguous run
#: of at least this fraction of the page width is what actually separates
#: rules from text, and it discards the em-dashes in the prose besides.
_MIN_RULE_RUN_FRACTION = 0.20
#: Scanning noise and JPEG ringing punch small holes in a rule; bridge them
#: when measuring its length.
_RULE_RUN_GAP = 3
#: A band shorter than this cannot hold a text row; it is inter-rule spacing.
_MIN_BAND_HEIGHT = 25
#: More vertical "rules" than this in one band means the projection found
#: text stems, not ruling.
_MAX_VRULES_PER_BAND = 30
#: Vertical rules this far apart across bands still count as the same column.
_VRULE_MATCH_TOLERANCE = 6

#: Crop inset for per-cell OCR: enough to keep the ruling lines out of the
#: cell (tesseract reads a rule as punctuation) without clipping glyphs.
_CELL_INSET = 6
#: Margin around a table when whiting it out for the prose pass.
_TABLE_WHITEOUT_MARGIN = 4
#: Dark fraction above which an apparently textless cell is holding ink the
#: engine failed to read. Deliberately low: this flag exists to make loss
#: visible, and a false positive costs a review-queue entry while a false
#: negative costs a silently dropped value.
_INK_WITHOUT_TEXT_FRACTION = 0.02

_CELL_CONFIG = f"--oem 3 --psm 6 --dpi {RENDER_DPI}"
_PAGE_CONFIG = f"--oem 3 --psm 3 --dpi {RENDER_DPI}"

_LOW_CONFIDENCE = Decimal("0.8")
_HUNDRED = Decimal(100)


@dataclass(frozen=True)
class _OcrWord:
    """One tesseract word box, in image pixels."""

    text: str
    left: int
    top: int
    width: int
    height: int
    confidence: Decimal
    line_num: int
    word_num: int


@dataclass(frozen=True)
class _Band:
    """The horizontal strip between two adjacent horizontal rules."""

    top: int
    bottom: int
    vrules: list[int]


@dataclass(frozen=True)
class _TableGrid:
    """A run of bands sharing a vertical-rule set: rows x column edges."""

    bands: list[_Band]
    vrules: list[int]

    @property
    def top(self) -> int:
        return self.bands[0].top

    @property
    def bottom(self) -> int:
        return self.bands[-1].bottom


class TesseractExtractor:
    """TableExtractor over the local tesseract binary. $0, zero API calls."""

    @property
    def engine(self) -> str:
        return "tesseract"

    def extract_pages(self, pdf_bytes: bytes, page_numbers: Sequence[int]) -> PageBatch:
        tesseract = self._pytesseract()
        pages: list[PageExtraction] = []
        pdf = pdfium.PdfDocument(pdf_bytes)
        try:
            for number in page_numbers:
                pages.append(self._extract_page(pdf, number, tesseract))
        finally:
            pdf.close()
        # A local binary is not a hosted service: no calls, no spend. This is
        # the whole reason the local target's cost table reads $0 everywhere.
        return PageBatch(pages=pages, api_calls=0, usd=Decimal(0))

    @staticmethod
    def _pytesseract() -> ModuleType:
        """Import pytesseract lazily.

        It lives behind the ``ocr`` extra because the tesseract binary cannot
        exist in a Vercel function bundle; importing it at module scope would
        make merely *referencing* the local adapter fail on that target.
        """
        try:
            import pytesseract
        except ImportError as exc:  # pragma: no cover - exercised by env, not tests
            raise ImportError(
                "TesseractExtractor needs the optional 'ocr' extra and the tesseract "
                "binary on PATH. Install with `uv sync --extra ocr` and, on macOS, "
                "`brew install tesseract`. This adapter is local-only: on Vercel use "
                "the vision-OCR adapter, on AWS use Textract."
            ) from exc
        # pytesseract ships no stubs, so the import lands as Any; the cast
        # keeps the Any confined to this one line.
        return cast("ModuleType", pytesseract)

    def _extract_page(self, pdf: Any, number: int, tesseract: ModuleType) -> PageExtraction:
        page = pdf[number - 1]
        width_pt, height_pt = (float(v) for v in page.get_size())
        scale = RENDER_DPI / _PDF_POINTS_PER_INCH
        image = page.render(scale=scale).to_pil().convert("RGB")

        gray = image.convert("L")
        angle = _estimate_skew(gray)
        if abs(angle) > _MIN_CORRECTED_SKEW:
            # expand=False keeps the pixel grid identical, so the px -> pt
            # mapping established below survives the rotation unchanged.
            image = image.rotate(
                angle, resample=Image.Resampling.BICUBIC, fillcolor=(255, 255, 255)
            )
            gray = image.convert("L")

        ink = _ink_mask(gray, _otsu_threshold(gray))
        grids = _detect_tables(ink)

        # The render is a full-page, unrotated, uniformly scaled bitmap, so
        # image pixels map to PDF points by one proportional factor on both
        # axes — exactly, not approximately.
        to_points = width_pt / image.width

        words: list[_OcrWord] = []
        tables: list[ExtractedTable] = []
        for index, grid in enumerate(grids):
            table, cell_words = _ocr_table(image, ink, grid, tesseract)
            words.extend(cell_words)
            tables.append(
                ExtractedTable(
                    page_number=number,
                    table_id=f"p{number}_t{index}",
                    bbox=(
                        grid.vrules[0] * to_points,
                        grid.top * to_points,
                        grid.vrules[-1] * to_points,
                        grid.bottom * to_points,
                    ),
                    grid_source=GridSource.RULED_CELL_OCR,
                    rows=table,
                    column_count=len(grid.vrules) - 1,
                )
            )

        prose_words = _ocr_prose(image, grids, tesseract)
        words.extend(prose_words)
        prose = _prose_blocks(prose_words, page_number=number, to_points=to_points)

        return PageExtraction(
            page_number=number,
            width=width_pt,
            height=height_pt,
            method=ExtractionMethod.OCR,
            tables=tables,
            prose=prose,
            ocr_stats=_page_stats(words),
        )


# --------------------------------------------------------------------------
# Image analysis (pure, no tesseract)
# --------------------------------------------------------------------------


def _row_profile(gray: Image.Image) -> list[float]:
    """Mean intensity per image row, via a one-pixel-wide BOX resize."""
    column = gray.resize((1, gray.height), Image.Resampling.BOX)
    # tobytes() rather than getdata(): one byte per pixel for mode "L", no
    # row padding, and not deprecated.
    return [float(v) for v in column.tobytes()]


def _estimate_skew(gray: Image.Image) -> float:
    """Estimate page skew in degrees; positive means counter-clockwise.

    Text lines are the strongest periodic structure on the page, so the
    row-projection profile has maximum variance when they are horizontal:
    every row is then either all-text or all-whitespace. The search runs on a
    quarter-scale copy because rotating and projecting the full 2550x3301
    render 41 times is pointlessly expensive at this precision.

    The angle is always measured, never assumed: a hardcoded correction is
    right for exactly one scan.
    """
    small = gray.resize((max(1, gray.width // 4), max(1, gray.height // 4)), Image.Resampling.BOX)
    best_angle, best_score = 0.0, -1.0
    for tenths in range(-_SKEW_LIMIT_TENTHS, _SKEW_LIMIT_TENTHS + 1):
        angle = tenths / 10.0
        rotated = small.rotate(angle, resample=Image.Resampling.BILINEAR, fillcolor=255)
        score = pvariance(_row_profile(rotated))
        if score > best_score:
            best_angle, best_score = angle, score
    return best_angle


def _otsu_threshold(gray: Image.Image) -> int:
    """Otsu's threshold from the intensity histogram.

    Maximizes between-class variance over the 256 candidate splits. A fixed
    threshold of 160 also worked on this scan, but it is a constant fitted to
    one document; Otsu adapts to whatever exposure the next scan arrives with.
    """
    histogram = gray.histogram()[:256]
    total = sum(histogram)
    if total == 0:
        return 127
    weighted_total = sum(value * count for value, count in enumerate(histogram))
    below = 0
    weighted_below = 0.0
    best_threshold, best_variance = 127, -1.0
    for value, count in enumerate(histogram):
        below += count
        if below == 0:
            continue
        above = total - below
        if above == 0:
            break
        weighted_below += value * count
        mean_below = weighted_below / below
        mean_above = (weighted_total - weighted_below) / above
        variance = below * above * (mean_below - mean_above) ** 2
        if variance > best_variance:
            best_threshold, best_variance = value, variance
    return best_threshold


def _ink_mask(gray: Image.Image, threshold: int) -> Image.Image:
    """Binarize *and* invert: ink becomes 255, paper 0, so every projection
    below is a plain mean over the mask."""
    return gray.point([255 if value <= threshold else 0 for value in range(256)])


def _clusters(indexes: Iterable[int], max_gap: int) -> list[tuple[int, int]]:
    """Group sorted indexes into inclusive (start, end) runs."""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    previous = 0
    for index in indexes:
        if start is None:
            start, previous = index, index
        elif index - previous <= max_gap:
            previous = index
        else:
            runs.append((start, previous))
            start, previous = index, index
    if start is not None:
        runs.append((start, previous))
    return runs


def _longest_run(mask: bytes, max_gap: int) -> int:
    """Longest run of nonzero bytes, bridging gaps of up to ``max_gap``."""
    best = current = gap = 0
    started = False
    for value in mask:
        if value:
            current = current + gap + 1 if started else 1
            started, gap = True, 0
        elif started:
            gap += 1
            if gap > max_gap:
                best = max(best, current)
                started, current, gap = False, 0, 0
    return max(best, current)


def _horizontal_rules(ink: Image.Image) -> list[tuple[int, int]]:
    """Horizontal ruling lines as inclusive (top, bottom) pixel runs.

    A candidate cluster must be thin (a thick band is a filled region, not a
    rule) *and* continuous across a fifth of the page. The continuity test is
    the load-bearing one: without it the bold header row of fixture 05's
    Table 1 registers as two rules and cuts the header cells into slivers
    that OCR as noise.
    """
    profile = _row_profile(ink)
    dark = [y for y, mean in enumerate(profile) if mean / 255.0 >= _HRULE_INK_FRACTION]
    minimum_run = ink.width * _MIN_RULE_RUN_FRACTION
    rules: list[tuple[int, int]] = []
    for top, bottom in _clusters(dark, _RULE_CLUSTER_GAP):
        if bottom - top + 1 > _MAX_RULE_THICKNESS:
            continue
        # Measure on the densest row of the cluster: after deskewing, a real
        # rule has one row that is essentially solid.
        densest = max(range(top, bottom + 1), key=lambda y: profile[y])
        row = ink.crop((0, densest, ink.width, densest + 1)).tobytes()
        if _longest_run(row, _RULE_RUN_GAP) >= minimum_run:
            rules.append((top, bottom))
    return rules


def _vertical_rules(ink: Image.Image, top: int, bottom: int) -> list[int]:
    """Vertical ruling lines within one band, as centre x positions.

    A band yielding an implausible number of them is showing text stems, not
    ruling, and contributes nothing.
    """
    band = ink.crop((0, top, ink.width, bottom))
    row = band.resize((ink.width, 1), Image.Resampling.BOX)
    dark = [x for x, mean in enumerate(row.tobytes()) if mean / 255.0 >= _VRULE_INK_FRACTION]
    runs = _clusters(dark, _RULE_CLUSTER_GAP)
    if len(runs) > _MAX_VRULES_PER_BAND:
        return []
    return [(start + end) // 2 for start, end in runs]


def _bands(ink: Image.Image) -> list[_Band]:
    """The strips between adjacent horizontal rules, in page order.

    Short strips are kept with no vertical rules rather than dropped: they
    must still break a run of table rows, or two unrelated tables stacked on
    one page could merge.
    """
    rules = _horizontal_rules(ink)
    bands: list[_Band] = []
    for (_, upper), (lower, _) in pairwise(rules):
        top, bottom = upper + 1, lower - 1
        if bottom - top + 1 < _MIN_BAND_HEIGHT:
            bands.append(_Band(top=top, bottom=bottom, vrules=[]))
        else:
            bands.append(_Band(top=top, bottom=bottom, vrules=_vertical_rules(ink, top, bottom)))
    return bands


def _vrules_match(left: list[int], right: list[int]) -> bool:
    return len(left) == len(right) and all(
        abs(a - b) <= _VRULE_MATCH_TOLERANCE for a, b in zip(left, right, strict=True)
    )


def _detect_tables(ink: Image.Image) -> list[_TableGrid]:
    """A maximal run of >= 2 consecutive bands sharing >= 2 consistent
    vertical rules is one table; its rows are the bands and its columns the
    gaps between the rules."""
    bands = _bands(ink)
    grids: list[_TableGrid] = []
    index = 0
    while index < len(bands):
        anchor = bands[index]
        if len(anchor.vrules) < 2:
            index += 1
            continue
        end = index + 1
        while end < len(bands) and _vrules_match(anchor.vrules, bands[end].vrules):
            end += 1
        run = bands[index:end]
        if len(run) >= 2:
            # Average each rule across the run: a per-band centre wobbles by a
            # pixel or two on a scan, and the cell crops should not.
            columns = [
                round(fmean(band.vrules[i] for band in run)) for i in range(len(anchor.vrules))
            ]
            grids.append(_TableGrid(bands=run, vrules=columns))
            index = end
        else:
            index += 1
    return grids


# --------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------


def _ocr_words(image: Image.Image, config: str, tesseract: ModuleType) -> list[_OcrWord]:
    """Run tesseract and keep only real words.

    ``level == 5`` is the word level; ``conf == -1`` marks the structural rows
    (page/block/paragraph/line) that carry no text of their own.
    """
    data: Any = tesseract.image_to_data(image, config=config, output_type=tesseract.Output.DICT)
    words: list[_OcrWord] = []
    for i in range(len(data["level"])):
        if int(data["level"][i]) != 5:
            continue
        text = str(data["text"][i]).strip()
        confidence = int(float(data["conf"][i]))
        if not text or confidence == -1:
            continue
        words.append(
            _OcrWord(
                text=text,
                left=int(data["left"][i]),
                top=int(data["top"][i]),
                width=int(data["width"][i]),
                height=int(data["height"][i]),
                confidence=Decimal(confidence) / _HUNDRED,
                line_num=int(data["line_num"][i]),
                word_num=int(data["word_num"][i]),
            )
        )
    return words


def _cell_box(grid: _TableGrid, band: _Band, column: int) -> tuple[int, int, int, int]:
    return (
        grid.vrules[column] + _CELL_INSET,
        band.top + _CELL_INSET,
        grid.vrules[column + 1] - _CELL_INSET,
        band.bottom - _CELL_INSET,
    )


def _has_ink(ink: Image.Image, box: tuple[int, int, int, int]) -> bool:
    left, top, right, bottom = box
    if right <= left or bottom <= top:
        return False
    region = ink.crop(box)
    pixels = region.width * region.height
    return sum(region.tobytes()) / (255.0 * pixels) > _INK_WITHOUT_TEXT_FRACTION


def _ocr_table(
    image: Image.Image, ink: Image.Image, grid: _TableGrid, tesseract: ModuleType
) -> tuple[list[list[Cell]], list[_OcrWord]]:
    """OCR every cell of one table independently.

    A cell the engine reads as empty is not automatically empty: if the
    region still holds ink, the cell is flagged ``ink_without_text`` with
    confidence 0 so the value reaches the review queue instead of vanishing
    (anti-goal #8). A genuinely blank cell is confident about being blank.
    """
    rows: list[list[Cell]] = []
    collected: list[_OcrWord] = []
    for band in grid.bands:
        row: list[Cell] = []
        for column in range(len(grid.vrules) - 1):
            box = _cell_box(grid, band, column)
            left, top, right, bottom = box
            if right <= left or bottom <= top:
                row.append(Cell(text=""))
                continue
            words = _ocr_words(image.crop(box), _CELL_CONFIG, tesseract)
            if words:
                collected.extend(words)
                ordered = sorted(words, key=lambda w: (w.line_num, w.word_num))
                row.append(
                    Cell(
                        text=" ".join(w.text for w in ordered),
                        confidence=min(w.confidence for w in words),
                    )
                )
            elif _has_ink(ink, box):
                row.append(Cell(text="", confidence=Decimal(0), ink_without_text=True))
            else:
                row.append(Cell(text=""))
        rows.append(row)
    return rows, collected


def _ocr_prose(
    image: Image.Image, grids: list[_TableGrid], tesseract: ModuleType
) -> list[_OcrWord]:
    """Page-level OCR with the tables whited out.

    Removing the ruling lines is what lets ``--psm 3`` do its job: layout
    analysis on the full page is exactly what the tables defeated.
    """
    canvas = image.copy()
    draw = ImageDraw.Draw(canvas)
    for grid in grids:
        draw.rectangle(
            (
                grid.vrules[0] - _TABLE_WHITEOUT_MARGIN,
                grid.top - _TABLE_WHITEOUT_MARGIN,
                grid.vrules[-1] + _TABLE_WHITEOUT_MARGIN,
                grid.bottom + _TABLE_WHITEOUT_MARGIN,
            ),
            fill=(255, 255, 255),
        )
    return _ocr_words(canvas, _PAGE_CONFIG, tesseract)


def _prose_blocks(words: list[_OcrWord], *, page_number: int, to_points: float) -> list[ProseBlock]:
    """Group prose words into blocks, in PDF points.

    The same row/line/block grouping the digital adapter uses — the geometry
    is unit-agnostic, so converting the word boxes to points first means both
    paths produce blocks in the same coordinate space.
    """
    positioned = [
        Word(
            text=w.text,
            x0=w.left * to_points,
            x1=(w.left + w.width) * to_points,
            top=w.top * to_points,
            bottom=(w.top + w.height) * to_points,
            confidence=w.confidence,
        )
        for w in words
    ]
    blocks: list[ProseBlock] = []
    for block in group_lines_into_blocks(rows_to_lines(group_rows(positioned))):
        text = "\n".join(line.text for line in block)
        if not text.strip():
            continue
        blocks.append(
            ProseBlock(
                page_number=page_number,
                kind=classify_block(text),
                text=text,
                bbox=(
                    min(line.x0 for line in block),
                    min(line.top for line in block),
                    max(line.x1 for line in block),
                    max(line.bottom for line in block),
                ),
                confidence=min(line.confidence for line in block),
            )
        )
    return blocks


def _page_stats(words: list[_OcrWord]) -> OcrPageStats:
    """Coverage and tail statistics over *every* word the page produced —
    cell words and prose words alike.

    ``word_count`` is the dropout guard: fixture 05 holds roughly 275 words,
    so a run reporting 227 must look wrong in the extraction report rather
    than average well and pass (anti-goal #8).
    """
    if not words:
        return OcrPageStats(
            word_count=0,
            mean_confidence=Decimal(0),
            p10_confidence=Decimal(0),
            low_confidence_fraction=ONE,
        )
    confidences = [w.confidence for w in words]
    total = sum(confidences, start=Decimal(0))
    low = sum(1 for c in confidences if c < _LOW_CONFIDENCE)
    return OcrPageStats(
        word_count=len(words),
        mean_confidence=(total / Decimal(len(confidences))).quantize(Decimal("0.0001")),
        p10_confidence=percentile(confidences, Decimal("0.1")),
        low_confidence_fraction=(Decimal(low) / Decimal(len(confidences))).quantize(
            Decimal("0.0001")
        ),
    )
