"""API response models.

Decimals serialize as exact-digit JSON strings (pydantic v2 JSON mode):
``"0.062"`` stays ``"0.062"``. For tax data, exactness outranks a native
number type, and every consumer that parses money or rates should be doing
so decimally anyway — the OpenAPI schema documents the string form.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class IngestAccepted(BaseModel):
    """202 body: the upload was accepted; the pipeline runs asynchronously.
    ``duplicate`` is True when this PDF (by sha256) was already known and an
    existing job was returned instead of new work being started."""

    model_config = ConfigDict(frozen=True)

    document_id: UUID
    job_id: UUID
    duplicate: bool


class DocumentOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    sha256: str
    filename: str
    content_type: str
    byte_size: int
    page_count: int | None
    source_kind: str | None
    uploaded_at: datetime


class JobOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    document_id: UUID
    status: str
    attempt: int
    records_extracted: int | None
    records_persisted: int | None
    review_count: int | None
    error: dict[str, Any] | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RecordOut(BaseModel):
    """One canonical fact, bounds restored to the inclusive integers the
    corpus states (the range type's half-open form is storage detail)."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    document_id: UUID
    source_page: int
    table_id: str
    record_type: str
    jurisdiction: str
    attribute_key: str | None
    filing_status: str | None
    taxpayer_class: str | None
    tax_year: int | None
    effective_from: date | None
    effective_to: date | None
    lifecycle_status: str
    lower_bound: int | None
    upper_bound: int | None
    rate: Decimal | None
    amount: Decimal | None
    currency: str | None
    attrs: dict[str, Any]
    confidence: Decimal
    review_status: str
    created_at: datetime
    updated_at: datetime


class RecordsPage(BaseModel):
    """Cursor-paginated records. ``next_cursor`` is opaque; None means the
    walk is complete. Pass it back as ``?cursor=`` to continue."""

    model_config = ConfigDict(frozen=True)

    items: list[RecordOut]
    next_cursor: str | None = Field(default=None)


class ResolveOut(BaseModel):
    """The bracket record containing the queried amount — a data lookup,
    deliberately not shaped like (and never to be read as) a computed tax
    liability."""

    model_config = ConfigDict(frozen=True)

    amount: int
    record: RecordOut


class SweepOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    processed: list[UUID]
