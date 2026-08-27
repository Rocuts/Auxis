"""SchemaMapper port — interface only until Phase 2b.

The mapper is semantic: it receives an already-extracted grid and decides
what each cell *means* — which column is a rate, which filing status a
column belongs to, whether a dash means null. It never reads pixels and it
never invents a value: every numeric in a CanonicalRecord must trace to a
cell or prose block the extraction layer produced.

There is deliberately no adapter and no stub behind this protocol yet: the
real implementations (Bedrock on AWS, Anthropic API elsewhere) need an API
key that does not exist yet, and a fake mapper producing fabricated records
would poison the accuracy harness (anti-goal #1's neighbor). Phase 2b
implements it; nothing in Phase 2a may call it.

Provenance contract (owed to the accuracy harness and the review queue):
every CanonicalRecord keeps the extractor's ``ExtractedTable.table_id``
("p1_t0") unchanged in ``table_id`` — review-queue provenance stays in the
extraction key space — and additionally carries
``attrs["source_table_label"]``: the slug of the caption of the table (or
prose block) the record came from, lowercased with non-alphanumeric runs
collapsed to "_" ("Table A. ... (continued)" -> "table_a"), or "footnote"
for records derived from a ProseBlock of kind FOOTNOTE. Both values derive
from extracted content; neither may ever read the oracle.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from tax_tables.domain.records import CanonicalRecord
from tax_tables.extraction.model import ExtractedDocument


class MappingCost(BaseModel):
    """What mapping this document spent — the semantic-layer sibling of
    ``ExtractionCost``. Token counts are reported even when the model is
    unpriced, so cost is never silently zero without the evidence to check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    engine: str = Field(min_length=1)  # model id the call ran on
    api_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    usd: Decimal = Field(default=Decimal(0))
    wall_seconds: float = Field(default=0.0, ge=0)


class MappingIssue(BaseModel):
    """A cell or block the mapper could not confidently map — bound for the
    review queue with its provenance, never guessed at, never dropped."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_page: int = Field(ge=1)
    table_id: str | None = None
    row_index: int | None = Field(default=None, ge=0)
    col_index: int | None = Field(default=None, ge=0)
    raw_value: str | None = None
    reason: str = Field(min_length=1)


class MappingResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    records: list[CanonicalRecord]
    issues: list[MappingIssue]
    cost: MappingCost | None = None


class SchemaMapper(Protocol):
    def map_document(self, extracted: ExtractedDocument) -> MappingResult: ...
