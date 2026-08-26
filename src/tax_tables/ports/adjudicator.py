"""Adjudicator port — a single pass over open review-queue items (ADR 012).

The adjudicator re-examines one queued item at a time with the document's
full extracted evidence and proposes a citated resolution with a confidence.
It never edits records and never touches the fact table: the *pipeline*
applies the confidence threshold — at or above it the item auto-resolves
with a full audit trail; below it the item stays with a human and the
proposal is stored for the reviewer.

Citations are provenance references into the extracted document, validated
by the same rules as mapper provenance. ``citations_valid`` is set by the
adapter after that validation; a resolution whose citations are missing or
dangling must never auto-resolve, whatever its confidence claims.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from tax_tables.extraction.model import ExtractedDocument
from tax_tables.ports.mapper import MappingCost

#: Below this confidence (or with invalid citations at any confidence) an
#: adjudication is stored as a proposal and the item stays human.
DEFAULT_AUTO_RESOLVE_THRESHOLD = Decimal("0.9")


class AdjudicationError(RuntimeError):
    """An adjudication call failed (truncated, refused, malformed). Raised by
    adapters; the pipeline catches it PER ITEM — one failed adjudication
    leaves its item open and named, and never aborts the pass (anti-goal #8:
    the queue is the safety net, so the safety net must not have a crash
    path through it).

    ``cost`` carries the spend the failed call still incurred when a
    response was received before the failure was detected (a truncated or
    malformed body was paid for); a transport failure that never got a
    response leaves it None. Failed calls must not be free in the report."""

    def __init__(self, message: str, *, cost: MappingCost | None = None) -> None:
        super().__init__(message)
        self.cost = cost


class ReviewItem(BaseModel):
    """One open ``review_queue`` row, as the adjudicator sees it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    document_id: UUID
    source_page: int | None = None
    table_id: str | None = None
    row_index: int | None = None
    col_index: int | None = None
    raw_value: str | None = None
    reason: str = Field(min_length=1)


class Adjudication(BaseModel):
    """The adjudicator's proposed disposition of one review item."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: UUID
    resolution: str = Field(min_length=1)
    citations: list[dict[str, Any]]
    confidence: Decimal = Field(ge=Decimal(0), le=Decimal(1))
    #: True only when every citation resolves to a real cell/prose block of
    #: the extracted document AND at least one citation exists.
    citations_valid: bool
    cost: MappingCost | None = None

    def audit_payload(self) -> dict[str, Any]:
        """What lands in ``review_queue.resolution`` (jsonb) — for both an
        auto-resolution and a stored below-threshold proposal."""
        return {
            "resolution": self.resolution,
            "citations": self.citations,
            "confidence": str(self.confidence),
            "citations_valid": self.citations_valid,
            "engine": None if self.cost is None else self.cost.engine,
        }


class Adjudicator(Protocol):
    def adjudicate(self, item: ReviewItem, extracted: ExtractedDocument) -> Adjudication:
        """Re-examine one queued item against the full extracted evidence."""
        ...
