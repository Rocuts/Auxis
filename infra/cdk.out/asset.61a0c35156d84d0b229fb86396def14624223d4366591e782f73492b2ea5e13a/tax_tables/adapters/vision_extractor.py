"""Vercel OCR TableExtractor: a vision model reads the rendered page.

This adapter serves the **Vercel** target only. Tesseract is a system binary
and Vercel functions cannot install one, so without this adapter the one
scanned document in the corpus would be un-ingestable on the very target that
serves the live URL — a demo of the easy cases (ADR 010).

It does not bend the no-pixels rule; it is bound by the same one every other
extractor is. **The rule binds the ``SchemaMapper``**, which only ever sees an
extracted cell grid. ``TableExtractor`` adapters are the components licensed
to read pixels, and Textract — the AWS adapter — is itself a model reading
pixels. This is its platform equivalent, in the same port, behind the same
interface, emitting the same ``PageExtraction``. Nothing downstream can tell
which extractor ran.

Shape, deliberately identical to the Textract adapter so the two stay
comparable: one API call per page (the unit of work, of cost, and of
parallelism), pages rendered through the shared 300 DPI RGB renderer, and the
router deciding which pages arrive here at all — which is what keeps the
corpus headline true, that four of five documents never reach a paid engine.

Fidelity rules, mirroring the sibling adapters exactly:

- a merged-cell continuation is ``None``; a genuinely empty cell is ``""``.
  The prompt states this distinction because it is the one the mapper's
  serializer reads;
- a region with visible ink the model cannot read becomes ``text=""`` with
  ``confidence=0`` and ``ink_without_text=True`` — visible loss, never
  silent (anti-goal #8);
- prose outside every table travels in full, because three documents state
  facts only outside their tables (doc 05's surtax rate exists *only* in its
  NOTE block, and its tax year only in a sentence);
- a ragged row is recorded in ``irregular_row_indexes`` rather than force-fit
  or dropped;
- a truncated, refused, or unparseable response raises. A half-read page
  would read downstream as "read, with fewer rows".

Two honest differences from Textract, both consequences of the transport and
both stated rather than hidden:

**Geometry is model-estimated.** Textract returns service-computed bounding
boxes; a vision model can only judge them by eye. Boxes are therefore
requested normalized, validated, scaled to image pixels, and treated as
*advisory* — an invalid box degrades to the full page rather than raising,
because a bad rectangle must never cost a table. No value that reaches a
record derives from them: provenance is (page, table_id, row, column).

**Confidences are self-reported.** Tesseract and Textract report per-word
engine confidences; a model reports its own judgment. That is weaker
evidence, and it is why ``OcrPageStats.word_count`` still counts actual
tokens: a page that comes back with far fewer words than it plainly holds is
caught by coverage even when the model claims certainty.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import anthropic
import pypdfium2 as pdfium
from pydantic import ValidationError

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

_DEFAULT_MODEL = "claude-opus-5"
#: Claude Opus 5 list prices, USD per million tokens. Override per role — a
#: page read is a far smaller task than schema mapping, so this is exactly
#: the knob for pointing OCR at a cheaper model.
_DEFAULT_USD_PER_MTOK_IN = Decimal(5)
_DEFAULT_USD_PER_MTOK_OUT = Decimal(25)
_MTOK = Decimal(1_000_000)

_MAX_OUTPUT_TOKENS = 16_000
_REQUEST_TIMEOUT_SECONDS = 600.0

#: Anthropic's documented per-image request cap. A page over it raises here,
#: naming the page, rather than as an opaque API error.
_MAX_IMAGE_BYTES = 5 * 1024 * 1024

_QUANTUM = Decimal("0.0001")
_LOW_CONFIDENCE = Decimal("0.8")


class VisionOcrConfigError(RuntimeError):
    """The environment does not describe a usable vision-OCR endpoint."""


class VisionOcrError(RuntimeError):
    """The page read failed in a way that must abort the document run: a
    truncated or refused response, or a body that is not the contracted
    JSON. Never degraded into a partial page."""


@dataclass(frozen=True)
class VisionOcrConfig:
    # repr=False: a traceback must never render the credential (anti-goal #10).
    api_key: str = field(repr=False)
    model: str = _DEFAULT_MODEL
    base_url: str | None = None
    usd_per_mtok_in: Decimal = _DEFAULT_USD_PER_MTOK_IN
    usd_per_mtok_out: Decimal = _DEFAULT_USD_PER_MTOK_OUT
    max_output_tokens: int = _MAX_OUTPUT_TOKENS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> VisionOcrConfig:
        """Same fallback chain as the verifier and adjudicator: role-specific
        first, then the shared Anthropic names."""
        source = os.environ if env is None else env
        api_key = source.get("VISION_OCR_API_KEY") or source.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise VisionOcrConfigError(
                "no vision-OCR API key: set VISION_OCR_API_KEY or ANTHROPIC_API_KEY"
            )
        return cls(
            api_key=api_key,
            model=source.get("VISION_OCR_MODEL") or _DEFAULT_MODEL,
            base_url=source.get("VISION_OCR_BASE_URL") or source.get("ANTHROPIC_BASE_URL"),
            usd_per_mtok_in=Decimal(
                source.get("VISION_OCR_USD_PER_MTOK_IN") or str(_DEFAULT_USD_PER_MTOK_IN)
            ),
            usd_per_mtok_out=Decimal(
                source.get("VISION_OCR_USD_PER_MTOK_OUT") or str(_DEFAULT_USD_PER_MTOK_OUT)
            ),
            max_output_tokens=int(source.get("VISION_OCR_MAX_OUTPUT_TOKENS") or _MAX_OUTPUT_TOKENS),
        )


SYSTEM_PROMPT = """You transcribe one rendered page of a tax document into a \
cell grid. You are an OCR engine, not an analyst.

Absolute rules:
- Transcribe VERBATIM. Never normalize, expand, complete, or correct anything. \
A dash stays that exact dash character. "1,000" stays "1,000". "12.5" never \
becomes "12.5%". Currency symbols stay present or absent exactly as printed.
- NEVER infer a value you cannot see. If a region has ink you cannot read, \
emit that cell with text "" , confidence 0, and ink_without_text true. That is \
the correct answer; guessing is not.
- A cell position covered by a merged cell spanning from its left or above has \
text null. A cell that exists and is genuinely blank has text "". These are \
different and the distinction matters downstream.
- Every row of a table must have the same number of cells as the table's widest \
row, padding with null only for merged-cell continuation. If a row genuinely \
has a different arity, emit it as-is and list its index in irregular_rows.
- Transcribe ALL text outside tables into prose blocks: headings, body \
sentences, footnotes, notes. Some documents state a rate or an effective year \
only in a footnote, and losing it loses records.
- confidence is your honest per-cell certainty in [0,1] that you read the \
glyphs correctly. It is not a measure of whether the value looks plausible.
- Bounding boxes are normalized to [0,1] against the page image \
(x0, top, x1, bottom) and are approximate; never let a box influence what you \
transcribe."""

_USER_INSTRUCTION = (
    "Transcribe this page. Return every table as a cell grid and every piece of "
    "text outside the tables as a prose block."
)

_CELL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text", "confidence", "ink_without_text"],
    "properties": {
        "text": {"type": ["string", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "ink_without_text": {"type": "boolean"},
    },
}

_BBOX_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "number"},
    "minItems": 4,
    "maxItems": 4,
}

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["tables", "prose"],
    "properties": {
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["bbox", "rows", "irregular_rows"],
                "properties": {
                    "bbox": _BBOX_SCHEMA,
                    "rows": {
                        "type": "array",
                        "items": {"type": "array", "items": _CELL_SCHEMA},
                    },
                    "irregular_rows": {"type": "array", "items": {"type": "integer"}},
                },
            },
        },
        "prose": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "bbox", "confidence"],
                "properties": {
                    "text": {"type": "string"},
                    "bbox": _BBOX_SCHEMA,
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        },
    },
}


def _scale_bbox(raw: Any, width: int, height: int) -> tuple[float, float, float, float]:
    """Normalized model box -> image pixels, degrading to the full page.

    A malformed or inverted rectangle is advisory metadata being wrong, which
    must never cost a table (anti-goal #8). The full page is the honest
    fallback: it says "somewhere on this page", which is true.
    """
    page_box = (0.0, 0.0, float(width), float(height))
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return page_box
    try:
        x0, top, x1, bottom = (float(v) for v in raw)
    except (TypeError, ValueError):
        return page_box
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= top < bottom <= 1.0):
        return page_box
    return (x0 * width, top * height, x1 * width, bottom * height)


def _cell(raw: Any) -> Cell:
    if not isinstance(raw, Mapping):
        raise VisionOcrError(f"cell is not an object: {raw!r}")
    text = raw.get("text")
    if text is not None and not isinstance(text, str):
        raise VisionOcrError(f"cell text is neither a string nor null: {text!r}")
    try:
        return Cell(
            text=text,
            confidence=Decimal(str(raw.get("confidence", 1))).quantize(_QUANTUM),
            ink_without_text=bool(raw.get("ink_without_text", False)),
        )
    except (ValidationError, ArithmeticError) as exc:
        raise VisionOcrError(f"unusable cell {raw!r}: {exc}") from exc


def parse_page_payload(text: str, *, page_number: int, width: int, height: int) -> PageExtraction:
    """The contracted JSON -> one PageExtraction. Fails closed, loudly."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VisionOcrError(f"page {page_number}: response is not JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise VisionOcrError(f"page {page_number}: response is not a JSON object")

    tables: list[ExtractedTable] = []
    for index, raw_table in enumerate(payload.get("tables") or []):
        if not isinstance(raw_table, Mapping):
            raise VisionOcrError(f"page {page_number}: table {index} is not an object")
        rows = [[_cell(c) for c in row] for row in (raw_table.get("rows") or [])]
        if not rows:
            # A table with no rows is not a table; dropping it silently would
            # be the loss this project refuses, so say so.
            raise VisionOcrError(f"page {page_number}: table {index} has no rows")
        column_count = max(len(row) for row in rows)
        irregular = sorted(
            {int(i) for i in (raw_table.get("irregular_rows") or []) if isinstance(i, int)}
            | {i for i, row in enumerate(rows) if len(row) != column_count}
        )
        tables.append(
            ExtractedTable(
                page_number=page_number,
                table_id=f"p{page_number}_t{index}",
                bbox=_scale_bbox(raw_table.get("bbox"), width, height),
                grid_source=GridSource.VISION_MODEL,
                rows=rows,
                column_count=column_count,
                irregular_row_indexes=[i for i in irregular if 0 <= i < len(rows)],
            )
        )

    prose: list[ProseBlock] = []
    for raw_block in payload.get("prose") or []:
        if not isinstance(raw_block, Mapping):
            raise VisionOcrError(f"page {page_number}: prose block is not an object")
        body = raw_block.get("text")
        if not isinstance(body, str) or not body.strip():
            continue  # an empty block carries nothing; it is not a loss
        prose.append(
            ProseBlock(
                page_number=page_number,
                kind=classify_block(body),
                text=body,
                bbox=_scale_bbox(raw_block.get("bbox"), width, height),
                confidence=Decimal(str(raw_block.get("confidence", 1))).quantize(_QUANTUM),
            )
        )

    return PageExtraction(
        page_number=page_number,
        width=float(width),
        height=float(height),
        method=ExtractionMethod.OCR,
        tables=tables,
        prose=prose,
        ocr_stats=_page_stats(tables, prose),
    )


def _page_stats(tables: Sequence[ExtractedTable], prose: Sequence[ProseBlock]) -> OcrPageStats:
    """Coverage and tail statistics over what the model returned.

    ``word_count`` counts real whitespace tokens rather than trusting the
    model's own account of its work: it is the guard against silent dropout,
    where a page comes back confident and mostly empty.
    """
    confidences: list[Decimal] = [c for table in tables for c in table.cell_confidences]
    confidences.extend(block.confidence for block in prose)
    words = sum(
        len(cell.text.split())
        for table in tables
        for row in table.rows
        for cell in row
        if cell.text
    )
    words += sum(len(block.text.split()) for block in prose)
    if not confidences:
        return OcrPageStats(
            word_count=words,
            mean_confidence=Decimal(0),
            p10_confidence=Decimal(0),
            low_confidence_fraction=ONE,
        )
    mean = (sum(confidences, Decimal(0)) / Decimal(len(confidences))).quantize(_QUANTUM)
    low = Decimal(sum(1 for c in confidences if c < _LOW_CONFIDENCE)) / Decimal(len(confidences))
    return OcrPageStats(
        word_count=words,
        mean_confidence=mean,
        p10_confidence=percentile(confidences, Decimal("0.1")).quantize(_QUANTUM),
        low_confidence_fraction=low.quantize(_QUANTUM),
    )


class AnthropicVisionExtractor:
    """TableExtractor over a vision-capable Anthropic model (or any endpoint
    speaking the Messages API, such as the Vercel AI Gateway)."""

    def __init__(self, config: VisionOcrConfig, *, client: Any | None = None) -> None:
        # ``client`` is injectable for tests, exactly like the mapper's:
        # anything exposing ``messages.stream(**kwargs)`` with a
        # ``get_final_message()`` context manager qualifies.
        self._config = config
        self._client = client if client is not None else self._build_client(config)
        self._usd = Decimal(0)
        self._calls = 0

    @staticmethod
    def _build_client(config: VisionOcrConfig) -> anthropic.Anthropic:
        return anthropic.Anthropic(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=_REQUEST_TIMEOUT_SECONDS,
            max_retries=3,
        )

    @property
    def engine(self) -> str:
        return f"vision:{self._config.model}"

    def extract_pages(self, pdf_bytes: bytes, page_numbers: Sequence[int]) -> PageBatch:
        pages: list[PageExtraction] = []
        usd = Decimal(0)
        pdf = pdfium.PdfDocument(pdf_bytes)
        try:
            for number in page_numbers:
                image, width, height = render_page_png(
                    pdf, number, max_bytes=_MAX_IMAGE_BYTES, limit_label="vision request"
                )
                page, spent = self._read_page(image, number, width, height)
                pages.append(page)
                usd += spent
        finally:
            pdf.close()
        # One call per page, like every other engine, so the cost line is
        # arithmetic rather than an estimate.
        return PageBatch(pages=pages, api_calls=len(page_numbers), usd=usd)

    def _read_page(
        self, image: bytes, number: int, width: int, height: int
    ) -> tuple[PageExtraction, Decimal]:
        import base64

        with self._client.messages.stream(
            model=self._config.model,
            max_tokens=self._config.max_output_tokens,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    # One page per call, but a multi-page scan pays for the
                    # prompt once.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": base64.b64encode(image).decode("ascii"),
                            },
                        },
                        {"type": "text", "text": _USER_INSTRUCTION},
                    ],
                }
            ],
            output_config={"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}},
        ) as stream:
            message = stream.get_final_message()

        if message.stop_reason != "end_turn":
            raise VisionOcrError(
                f"page {number}: read ended with stop_reason={message.stop_reason!r}; "
                "refusing to parse a truncated or refused response"
            )
        body: str | None = None
        for block in message.content:
            candidate = getattr(block, "text", None) if block.type == "text" else None
            if candidate:
                body = candidate
                break
        if body is None:
            raise VisionOcrError(f"page {number}: response contains no text block")

        page = parse_page_payload(body, page_number=number, width=width, height=height)
        usage = message.usage
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        usd = (
            Decimal(input_tokens) * self._config.usd_per_mtok_in
            + Decimal(output_tokens) * self._config.usd_per_mtok_out
        ) / _MTOK
        return page, usd


def vision_extractor(env: Mapping[str, str] | None = None) -> AnthropicVisionExtractor:
    """Build the Vercel target's OCR adapter from the environment."""
    return AnthropicVisionExtractor(VisionOcrConfig.from_env(env))


__all__ = [
    "AnthropicVisionExtractor",
    "VisionOcrConfig",
    "VisionOcrConfigError",
    "VisionOcrError",
    "parse_page_payload",
    "vision_extractor",
]
