"""The extraction router — deterministic first, OCR only when unavoidable.

Four of five fixture documents have a text layer; running any OCR or paid
table-extraction service on them would be waste. The router classifies each
page and dispatches:

    usable text layer          -> deterministic extraction (pdfplumber), $0
    scanned page (or rotated)  -> the target's OCR adapter
    genuinely blank page       -> nothing to extract, nothing spent

Classification is per page, on raw evidence, never on filename or metadata:
``len(page.chars)`` (the primitive under extract_text, with overprinted
duplicates deduped) against a threshold, plus the presence of a page image.
A blank page routes to neither path — the image conjunct keeps a blank page
from triggering a paid OCR call. Rotated pages (/Rotate metadata) route to
OCR: pdfplumber's coordinate handling for them is unreliable, and a wrong
grid is worse than a slower one.

The invariant the tests pin: a page with a usable text layer is NEVER sent
to an OCR adapter.
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

#: Below this many deduplicated characters a page has no usable text layer.
#: Real content pages carry hundreds; a stray watermark char or page number
#: does not make a page extractable.
MIN_TEXT_CHARS = 50


def classify_page(page: Page) -> ExtractionMethod:
    if page.rotation not in (0, None):
        return ExtractionMethod.OCR
    char_count = len(page.dedupe_chars().chars)
    if char_count >= MIN_TEXT_CHARS:
        return ExtractionMethod.DETERMINISTIC_TEXT
    if page.images:
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
