"""RecordVerifier port — the independent second half of the semantic layer.

The verifier receives a document's mapped records plus the same extracted
grid/prose context the mapper saw, in its own context, and confirms or
disputes each record's values and provenance citations (ADR 012).

Independence contract: an adapter must never feed the verifier the mapper's
reasoning, prompt transcript, or confidence self-assessment — only the mapped
records and the extraction view. Agreement between two independent
derivations is evidence; a model reviewing its own transcript is an echo.

The verifier never corrects, drops, or adds a record (anti-goal #8). A
dispute is a reason: the pipeline persists the disputed record as
``needs_review`` and routes the reason to the review queue.

Fail-closed verdict rule: a ``VerificationResult`` carries exactly one
verdict per mapped record, in record order. A record the verifying model's
response did not cover is DISPUTED ("no verdict"), never silently confirmed —
silence is not assent.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tax_tables.extraction.model import ExtractedDocument
from tax_tables.ports.mapper import MappingCost, MappingResult


class Verdict(StrEnum):
    CONFIRMED = "confirmed"
    DISPUTED = "disputed"


class RecordVerdict(BaseModel):
    """The verifier's judgment of one mapped record, addressed by its index
    in the mapping batch (the same address space triage findings use)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_index: int = Field(ge=0)
    verdict: Verdict
    reason: str | None = None

    @model_validator(mode="after")
    def _disputes_carry_reasons(self) -> RecordVerdict:
        # A dispute without its why is useless to the review queue.
        if self.verdict is Verdict.DISPUTED and not self.reason:
            raise ValueError("a disputed verdict requires a reason")
        return self


class VerificationResult(BaseModel):
    """One verdict per mapped record, in record order, plus what the
    verification call spent. ``notes`` records response anomalies the
    adapter degraded (e.g. a verdict naming a record index that does not
    exist) — kept so nothing the model said disappears silently."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdicts: list[RecordVerdict]
    notes: list[str] = Field(default_factory=list)
    cost: MappingCost | None = None

    @property
    def disputed(self) -> list[RecordVerdict]:
        return [v for v in self.verdicts if v.verdict is Verdict.DISPUTED]

    @model_validator(mode="after")
    def _one_verdict_per_record_in_order(self) -> VerificationResult:
        indexes = [v.record_index for v in self.verdicts]
        if indexes != list(range(len(indexes))):
            raise ValueError("verdicts must cover record indexes 0..n-1 exactly once, in order")
        return self


class VerificationError(RuntimeError):
    """A verification call failed in a way that yields no usable verdict set —
    truncated, refused, or not the contracted JSON. Raised by adapters.

    Declared on the PORT, not in an adapter, because the pipeline has to catch
    it: a verifier that cannot answer must flag this document's records rather
    than lose them or silently bless them (``pipeline.unverified_findings``).
    A domain module importing an adapter to name its exception would be the
    hexagon leaking.

    ``cost`` carries the spend a failed call still incurred when a response
    WAS received before the failure was detected — a body that arrived and
    broke the contract was paid for. A transport failure that never got a
    response leaves it None. This mirrors ``AdjudicationError`` exactly; the
    asymmetry was found adversarially on document 04, where the verifier
    returned a malformed body, was billed for it, and the report showed
    ``ver_usd 0.0000``. Failed calls must not be free in the report.
    """

    def __init__(self, message: str, *, cost: MappingCost | None = None) -> None:
        super().__init__(message)
        self.cost = cost


class RecordVerifier(Protocol):
    def verify(self, extracted: ExtractedDocument, mapping: MappingResult) -> VerificationResult:
        """Judge every record of ``mapping`` against ``extracted``."""
        ...
