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

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from tax_tables.extraction.model import ExtractedDocument
from tax_tables.ports.mapper import MappingCost

#: Below this confidence (or with invalid citations at any confidence) an
#: adjudication is stored as a proposal and the item stays human.
DEFAULT_AUTO_RESOLVE_THRESHOLD = Decimal("0.9")


#: A number standing on its own, not digits embedded in an identifier. The
#: lookarounds matter: without them "p1_t0" and "row 3" contribute figures 1,
#: 0 and 3 that no cell will ever carry, and every resolution citing a table
#: coordinate would be refused auto-resolution for asserting them.
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])-?\d[\d,]*(?:\.\d+)?(?![A-Za-z0-9_])")


#: Grid coordinates a resolution names while pointing AT its evidence. They
#: are addresses, not claims about tax values, and counting them would refuse
#: any resolution that says where it looked.
_COORDINATE = re.compile(r"\b(?:rows?|cols?|columns?|pages?|prose_index|index)\s*#?\s*\d+", re.I)


def _figures(text: str) -> set[Decimal]:
    """Every number in ``text``, comma-stripped so "566,700" and "566700"
    compare equal."""
    figures: set[Decimal] = set()
    for match in _NUMBER.findall(text):
        try:
            figures.add(Decimal(match.replace(",", "")))
        except InvalidOperation:  # pragma: no cover - the regex admits none
            continue
    return figures


#: The only transforms a resolution may apply to its evidence, and both are
#: written in the mapper's own canonical conventions rather than invented here:
#: a rate printed "22%" maps to 0.22, and a bracket that starts where the one
#: below it ended is derived by one ("Over $566,700" -> lower_bound 566701).
def _reachable(asserted: Decimal, evidence: set[Decimal]) -> bool:
    if asserted in evidence:
        return True
    for value in evidence:
        if asserted == value / 100 or asserted == value * 100:
            return True
        if asserted == value + 1 or asserted == value - 1:
            return True
    return False


def cited_evidence(citations: Sequence[Any], extracted: ExtractedDocument) -> str:
    """The text of every cell and prose block a resolution cites."""
    tables = {table.table_id: table for table in extracted.tables}
    prose_by_page = {page.page_number: page.prose for page in extracted.pages}
    chunks: list[str] = []
    for ref in citations:
        if not isinstance(ref, Mapping):
            continue
        if ref.get("kind") == "cell":
            table_id = ref.get("table_id")
            row, col = ref.get("row"), ref.get("col")
            if not isinstance(table_id, str):
                continue
            table = tables.get(table_id)
            if table is None or not isinstance(row, int) or not isinstance(col, int):
                continue
            if 0 <= row < len(table.rows) and 0 <= col < len(table.rows[row]):
                cell = table.rows[row][col]
                if cell.text:
                    chunks.append(cell.text)
        elif ref.get("kind") == "prose":
            page_number = ref.get("page")
            if not isinstance(page_number, int):
                continue
            blocks = prose_by_page.get(page_number, [])
            index = ref.get("prose_index")
            if isinstance(index, int) and 0 <= index < len(blocks):
                chunks.append(blocks[index].text)
    return "\n".join(chunks)


def resolution_is_supported(
    resolution: str, citations: Sequence[Any], extracted: ExtractedDocument
) -> bool:
    """Do the cited cells mechanically carry every figure the resolution states?

    ``citations_valid`` only proves the cited cells EXIST. This asks the next
    question: does the evidence actually say what the resolution claims it
    says. Every number asserted in the prose must appear in some cited cell or
    prose block, or the item does not auto-close.

    Fail-closed, and bounded: a figure counts as supported only if it appears
    in the evidence outright or is reachable by one of the two transforms this
    schema documents — percent to fraction, and a bracket bound derived by one.
    Anything further from the evidence than that goes to a human, however
    confident the model was. A resolution stating no figures at all is not
    blocked by this rule; it is judged by the others.
    """
    asserted = _figures(_COORDINATE.sub(" ", resolution))
    if not asserted:
        return True
    evidence = _figures(cited_evidence(citations, extracted))
    return all(_reachable(figure, evidence) for figure in asserted)


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
