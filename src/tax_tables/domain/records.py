"""Canonical record model.

Conventions mirror the fixture corpus's documented target schema:
- rate is a decimal fraction (0.22 = 22%); *_pct attributes are percentages
- bracket bounds are inclusive whole-currency integers; upper_bound None on a
  bracket record means the bracket is open-ended ("and over")
- lifecycle_status 'superseded' records must not surface in tax_year=2026
  queries; the status is declared by document content, never by arrival order
- values that only exist for some record types (rate triples, *_pct columns,
  prior-year amounts, prose rules) live in ``attrs``
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RecordType(StrEnum):
    ORDINARY_INCOME_BRACKET = "ordinary_income_bracket"
    PREFERENTIAL_GAIN_BRACKET = "preferential_gain_bracket"
    SPECIAL_GAIN_RATE = "special_gain_rate"
    STANDARD_DEDUCTION = "standard_deduction"
    ADDITIONAL_STANDARD_DEDUCTION = "additional_standard_deduction"
    DEPENDENT_DEDUCTION_RULE = "dependent_deduction_rule"
    SALES_TAX_RATE = "sales_tax_rate"
    EMPLOYMENT_TAX_RATE = "employment_tax_rate"
    WAGE_BASE = "wage_base"
    SURTAX_THRESHOLD = "surtax_threshold"
    WITHHOLDING_ALLOWANCE = "withholding_allowance"


class FilingStatus(StrEnum):
    SINGLE = "single"
    MARRIED_FILING_JOINTLY = "married_filing_jointly"
    MARRIED_FILING_SEPARATELY = "married_filing_separately"
    HEAD_OF_HOUSEHOLD = "head_of_household"
    # Documents 02 and 04 carry "Qualifying surviving spouse" rows; missing
    # this member made two legitimate records unpersistable (migration 0005).
    QUALIFYING_SURVIVING_SPOUSE = "qualifying_surviving_spouse"


class LifecycleStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class ReviewStatus(StrEnum):
    CLEAN = "clean"
    NEEDS_REVIEW = "needs_review"


class CanonicalRecord(BaseModel):
    """One extracted fact, with provenance, ready to persist.

    Immutable and strictly validated: a record either satisfies the canonical
    shape or is never constructed — a malformed cell belongs in the review
    queue, not in a half-valid model.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    # Provenance (document identity is supplied at ingest time by the caller).
    source_page: int = Field(ge=1)
    table_id: str = Field(min_length=1)

    # Discriminators.
    record_type: RecordType
    jurisdiction: str = Field(min_length=2)
    attribute_key: str | None = None
    filing_status: FilingStatus | None = None
    taxpayer_class: str | None = None

    # Temporal validity.
    tax_year: int | None = Field(default=None, ge=1900, le=2999)
    effective_from: date | None = None
    effective_to: date | None = None
    lifecycle_status: LifecycleStatus = LifecycleStatus.ACTIVE

    # Value slots.
    lower_bound: int | None = Field(default=None, ge=0)
    upper_bound: int | None = Field(default=None, ge=0)
    rate: Decimal | None = Field(default=None, gt=Decimal("-1"), lt=Decimal("1"))
    amount: Decimal | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    attrs: dict[str, Any] = Field(default_factory=dict)

    # Pipeline quality.
    confidence: Decimal = Field(ge=Decimal(0), le=Decimal(1))
    review_status: ReviewStatus = ReviewStatus.CLEAN

    @property
    def is_bracket(self) -> bool:
        return self.lower_bound is not None

    @model_validator(mode="after")
    def _validate_shape(self) -> CanonicalRecord:
        if self.upper_bound is not None and self.lower_bound is None:
            raise ValueError("upper_bound without lower_bound is not a bracket")
        if (
            self.lower_bound is not None
            and self.upper_bound is not None
            and self.upper_bound < self.lower_bound
        ):
            raise ValueError("upper_bound must be >= lower_bound")
        if self.is_bracket and (
            self.tax_year is None or (self.filing_status is None and self.taxpayer_class is None)
        ):
            # Document 01's Estates and Trusts schedule: brackets discriminated
            # by taxpayer_class alone, with no filing status (migration 0006).
            raise ValueError(
                "a bracket record requires tax_year and at least one taxpayer "
                "discriminator (filing_status or taxpayer_class)"
            )
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to < self.effective_from
        ):
            raise ValueError("effective_to must be >= effective_from")
        return self
