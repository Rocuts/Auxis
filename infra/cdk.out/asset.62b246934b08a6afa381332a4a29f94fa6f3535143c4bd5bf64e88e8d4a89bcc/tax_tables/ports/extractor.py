"""TableExtractor port.

Adapters per target: pdfplumber (all targets, digital pages), Textract
(AWS, scanned pages), vision-OCR via the Anthropic API (Vercel, scanned
pages), tesseract (local, scanned pages).

An adapter reads *pages* — the router decides which pages it gets, so a
document with a text layer can never reach a paid OCR engine by accident.
TableExtractor adapters are the only components licensed to read pixels;
everything downstream sees only the extracted grid.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from tax_tables.extraction.model import PageExtraction


class PageBatch(BaseModel):
    """Pages an adapter extracted, plus what doing so cost."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pages: list[PageExtraction]
    api_calls: int = Field(default=0, ge=0)
    usd: Decimal = Field(default=Decimal(0), ge=Decimal(0))


class TableExtractor(Protocol):
    @property
    def engine(self) -> str: ...

    def extract_pages(self, pdf_bytes: bytes, page_numbers: Sequence[int]) -> PageBatch:
        """Extract the given 1-based pages into cell grids + prose blocks.

        Must be faithful: verbatim cell text, no interpretation, no dropped
        content. A region an engine cannot read becomes a flagged low-
        confidence cell, never an omission (anti-goal #8).
        """
        ...
