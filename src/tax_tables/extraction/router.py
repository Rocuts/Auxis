"""The extraction router — deterministic first, OCR only when unavoidable.

Four of five fixture documents have a text layer; running any OCR or paid
table-extraction service on them would be waste. The router classifies each
page and dispatches:

    usable text layer          -> deterministic extraction (pdfplumber), $0
    scanned page (or rotated)  -> the target's OCR adapter
    genuinely blank page       -> nothing to extract, nothing spent

Classification is per page, on raw evidence, never on filename or metadata:
upright character count (overprinted duplicates deduped) against a
threshold, the presence of a page image, and whether any image dominates
the sheet. A blank page routes to neither path — the image conjunct keeps a
blank page from triggering a paid OCR call.

Two invariants, one per direction, both pinned by tests:

- a page with a usable text layer is NEVER sent to an OCR adapter (OCR
  costs money on two of three targets);
- a page dominated by a page-sized image is NEVER handed to the
  deterministic adapter, even when it also carries a small text layer.
  A scanner stamp or e-file header over 50 chars would otherwise classify
  the page as digital, pdfplumber would find no tables, and the document
  would come back empty at confidence 1.0 — anti-goal #8's silent loss
  (found by adversarial review, reproduced with a stamped-scan probe).

Orientation, not /Rotate metadata, decides "usable": pdfplumber resolves
page rotation before setting each char's ``upright``, so a rotated page
whose text reads upright stays on the $0 deterministic path (the brief
forbids sending any usable text layer to the paid vision adapter), while a
sideways text layer — which the deterministic adapter cannot read reliably
— goes to the pixel-licensed port.
"""

from __future__ import annotations

import hashlib
import io
import time
from typing import TYPE_CHECKING

import pdfplumber

from tax_tables.extraction.model import (
    ExtractedDocument,
    ExtractionCost,
    ExtractionMethod,
    PageExtraction,
)
from tax_tables.ports.extractor import TableExtractor

if TYPE_CHECKING:
    from pdfplumber.page import Page

#: Below this many deduplicated upright characters a page has no usable
#: text layer. Real content pages carry hundreds; a stray watermark char or
#: page number does not make a page extractable.
MIN_TEXT_CHARS = 50

#: A page whose image covers this fraction of the sheet is a scan. Its text
#: layer, if any, is an overlay — a Bates stamp, an e-file header — not the
#: page's content, and MIN_TEXT_CHARS alone does not screen it out: a
#: 60-char stamp clears the threshold and would send a whole scanned page
#: to pdfplumber, which finds no tables and reports the document empty.
_PAGE_IMAGE_COVERAGE = 0.5


def _scan_like(page: Page) -> bool:
    area = float(page.width) * float(page.height)
    if area <= 0:
        return False
    return any(
        abs(float(im["x1"]) - float(im["x0"])) * abs(float(im["bottom"]) - float(im["top"]))
        >= _PAGE_IMAGE_COVERAGE * area
        for im in page.images
    )


def classify_page(page: Page) -> ExtractionMethod:
    chars = page.dedupe_chars().chars
    upright_count = sum(1 for c in chars if c["upright"])
    if upright_count >= MIN_TEXT_CHARS and not _scan_like(page):
        return ExtractionMethod.DETERMINISTIC_TEXT
    if page.images or len(chars) >= MIN_TEXT_CHARS:
        # A scan (with or without an overlay stamp), or a sideways text
        # layer: only the pixel-licensed port can read it faithfully.
        return ExtractionMethod.OCR
    return ExtractionMethod.NONE


def route(pdf_bytes: bytes) -> list[ExtractionMethod]:
    """Per-page routing decision, 1-based order preserved."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return [classify_page(page) for page in pdf.pages]


class ExtractionRouter:
    """Dispatches pages to the digital and OCR adapters and reassembles one
    ExtractedDocument, with per-document cost accounting."""

    def __init__(self, *, digital: TableExtractor, ocr: TableExtractor) -> None:
        self._digital = digital
        self._ocr = ocr

    def extract(self, pdf_bytes: bytes, *, filename: str) -> ExtractedDocument:
        started = time.perf_counter()
        methods = route(pdf_bytes)
        blank_pages: dict[int, PageExtraction] = {}
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for number, (page, method) in enumerate(zip(pdf.pages, methods, strict=True), 1):
                if method is ExtractionMethod.NONE:
                    blank_pages[number] = PageExtraction(
                        page_number=number,
                        width=float(page.width),
                        height=float(page.height),
                        method=ExtractionMethod.NONE,
                    )

        by_page: dict[int, PageExtraction] = dict(blank_pages)
        engines: list[str] = []
        api_calls = 0
        usd = ExtractionCost.model_fields["usd"].get_default()
        for adapter, method in (
            (self._digital, ExtractionMethod.DETERMINISTIC_TEXT),
            (self._ocr, ExtractionMethod.OCR),
        ):
            numbers = [n for n, m in enumerate(methods, 1) if m is method]
            if not numbers:
                continue
            batch = adapter.extract_pages(pdf_bytes, numbers)
            engines.append(adapter.engine)
            api_calls += batch.api_calls
            usd += batch.usd
            for extracted_page in batch.pages:
                by_page[extracted_page.page_number] = extracted_page

        missing = set(range(1, len(methods) + 1)) - set(by_page)
        if missing:  # an adapter silently skipped pages: loud failure, not a gap
            raise RuntimeError(f"adapter returned no extraction for pages {sorted(missing)}")

        return ExtractedDocument(
            filename=filename,
            sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            pages=[by_page[n] for n in sorted(by_page)],
            cost=ExtractionCost(
                engine="+".join(engines) if engines else "none",
                api_calls=api_calls,
                usd=usd,
                wall_seconds=time.perf_counter() - started,
            ),
        )
