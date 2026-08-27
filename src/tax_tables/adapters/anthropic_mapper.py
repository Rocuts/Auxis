"""SchemaMapper adapter for the Anthropic Messages API.

One adapter serves both live targets: endpoint, key, and model are read from
the environment (``SCHEMA_MAPPER_*`` first, then the SDK's own
``ANTHROPIC_*`` names), so pointing it at the Vercel AI Gateway's
Anthropic-compatible endpoint is a configuration change, not a code change.

The mapper is semantic only (see the port docstring): it receives the
extracted grid and decides what each cell means. Three properties are
enforced here rather than hoped for:

- **No float ever touches a value.** The model's JSON is parsed with
  ``parse_float=Decimal``, the same rule the accuracy harness applies to its
  own expected values, so ``0.1`` in the response is ``Decimal("0.1")``.
- **A malformed proposal is an issue, not a crash and not a drop.** Any
  record the model emits that fails canonical validation (inverted bounds,
  non-integral bracket bound, dangling provenance) becomes a
  ``MappingIssue`` with its provenance attached (anti-goal #8); the rest of
  the batch is unaffected.
- **Traceability is structural.** Every record must name the cells or prose
  blocks it was read from; the references are checked against the extracted
  document and ride into ``attrs["provenance"]`` for the review queue.

The serialized input deliberately excludes the filename: tax_year must be
unlearnable from anything except document content.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import anthropic
from pydantic import ValidationError

from tax_tables.adapters.envelope import adapt_extra_attrs, adapt_numeric, loads_fence_tolerant
from tax_tables.adapters.pricing import ANTHROPIC_CACHE_FACTORS, cache_factors_for
from tax_tables.adapters.retry import (
    DEFAULT_CONTRACT_RETRIES,
    DEFAULT_CONTRACT_RETRY_SECONDS,
    DEFAULT_TRANSPORT_RETRY_SECONDS,
    with_bounded_retries,
)
from tax_tables.domain.records import (
    ATTRIBUTE_KEY_FIELD,
    CanonicalRecord,
    FilingStatus,
    LifecycleStatus,
    RecordType,
    ReviewStatus,
)
from tax_tables.extraction.model import ExtractedDocument
from tax_tables.observability import conformance
from tax_tables.ports.mapper import MappingCost, MappingIssue, MappingResult

_DEFAULT_MODEL = "claude-opus-5"
#: Claude Opus 5 list prices, USD per million tokens; override via env for
#: any other model or a gateway with different billing.
_DEFAULT_USD_PER_MTOK_IN = Decimal(5)
_DEFAULT_USD_PER_MTOK_OUT = Decimal(25)
_MTOK = Decimal(1_000_000)

_MAX_OUTPUT_TOKENS = 64_000
_REQUEST_TIMEOUT_SECONDS = 900.0


class MapperConfigError(RuntimeError):
    """The environment does not describe a usable mapping endpoint."""


class MapperError(RuntimeError):
    """The mapping call failed in a way that must abort the document run —
    a truncated or refused response, or a body that is not the contracted
    JSON. Never swallowed into a partial result: a half-mapped document
    would read as 'mapped, with fewer records' downstream."""


@dataclass(frozen=True)
class MapperConfig:
    # repr=False: a traceback or log line carrying the config must never
    # render the credential (anti-goal #10).
    api_key: str = field(repr=False)
    model: str = _DEFAULT_MODEL
    base_url: str | None = None
    usd_per_mtok_in: Decimal = _DEFAULT_USD_PER_MTOK_IN
    usd_per_mtok_out: Decimal = _DEFAULT_USD_PER_MTOK_OUT
    max_output_tokens: int = _MAX_OUTPUT_TOKENS
    #: Cache prices as multiples of the input price. Provider-aware rather
    #: than Anthropic-shaped: see ``adapters.pricing``. Defaults follow the
    #: configured model, so a gateway model id prices its own cache reads
    #: correctly on the first run, with no extra environment.
    cache_read_factor: Decimal = ANTHROPIC_CACHE_FACTORS.read
    cache_write_factor: Decimal = ANTHROPIC_CACHE_FACTORS.write
    #: Bounded retries on a contract failure (see ``adapters.retry``). Every
    #: attempt is counted, so retries lower the measured rate rather than
    #: hiding behind it.
    contract_retries: int = DEFAULT_CONTRACT_RETRIES
    contract_retry_seconds: float = DEFAULT_CONTRACT_RETRY_SECONDS
    transport_retry_seconds: float = DEFAULT_TRANSPORT_RETRY_SECONDS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> MapperConfig:
        source = os.environ if env is None else env
        api_key = source.get("SCHEMA_MAPPER_API_KEY") or source.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise MapperConfigError(
                "no mapping API key: set SCHEMA_MAPPER_API_KEY or ANTHROPIC_API_KEY"
            )
        model = source.get("SCHEMA_MAPPER_MODEL") or _DEFAULT_MODEL
        factors = cache_factors_for(model)
        return cls(
            api_key=api_key,
            model=model,
            base_url=source.get("SCHEMA_MAPPER_BASE_URL") or source.get("ANTHROPIC_BASE_URL"),
            usd_per_mtok_in=Decimal(
                source.get("SCHEMA_MAPPER_USD_PER_MTOK_IN") or str(_DEFAULT_USD_PER_MTOK_IN)
            ),
            usd_per_mtok_out=Decimal(
                source.get("SCHEMA_MAPPER_USD_PER_MTOK_OUT") or str(_DEFAULT_USD_PER_MTOK_OUT)
            ),
            max_output_tokens=int(
                source.get("SCHEMA_MAPPER_MAX_OUTPUT_TOKENS") or _MAX_OUTPUT_TOKENS
            ),
            cache_read_factor=Decimal(
                source.get("SCHEMA_MAPPER_CACHE_READ_FACTOR") or str(factors.read)
            ),
            cache_write_factor=Decimal(
                source.get("SCHEMA_MAPPER_CACHE_WRITE_FACTOR") or str(factors.write)
            ),
            contract_retries=int(
                source.get("SCHEMA_MAPPER_CONTRACT_RETRIES") or DEFAULT_CONTRACT_RETRIES
            ),
            contract_retry_seconds=float(
                source.get("SCHEMA_MAPPER_CONTRACT_RETRY_SECONDS") or DEFAULT_CONTRACT_RETRY_SECONDS
            ),
            transport_retry_seconds=float(
                source.get("SCHEMA_MAPPER_TRANSPORT_RETRY_SECONDS")
                or DEFAULT_TRANSPORT_RETRY_SECONDS
            ),
        )


# ---------------------------------------------------------------------------
# Input serialization
# ---------------------------------------------------------------------------


def serialize_document(extracted: ExtractedDocument) -> str:
    """The mapper's entire view of the document, as deterministic JSON.

    Verbatim by construction: cell text is passed through untouched, a
    merged-cell continuation stays ``null``, an empty cell stays ``""``.
    Cells whose extraction was doubtful are listed in ``cell_notes`` so the
    model knows which values deserve an issue instead of trust. The
    filename and sha256 are deliberately absent.
    """
    pages: list[dict[str, Any]] = []
    for page in extracted.pages:
        tables: list[dict[str, Any]] = []
        for table in page.tables:
            notes: list[dict[str, Any]] = []
            for row_index, row in enumerate(table.rows):
                for col_index, cell in enumerate(row):
                    if cell.text is None:
                        continue
                    note: dict[str, Any] = {}
                    if cell.confidence < Decimal(1):
                        note["confidence"] = str(cell.confidence)
                    if cell.ink_without_text:
                        note["ink_without_text"] = True
                    if note:
                        notes.append({"row": row_index, "col": col_index, **note})
            tables.append(
                {
                    "table_id": table.table_id,
                    "grid_source": table.grid_source.value,
                    "column_count": table.column_count,
                    "irregular_row_indexes": table.irregular_row_indexes,
                    "rows": [[cell.text for cell in row] for row in table.rows],
                    "cell_notes": notes,
                }
            )
        prose = [
            {
                "index": index,
                "kind": block.kind.value,
                "text": block.text,
                **({"confidence": str(block.confidence)} if block.confidence < Decimal(1) else {}),
            }
            for index, block in enumerate(page.prose)
        ]
        pages.append({"page_number": page.page_number, "tables": tables, "prose": prose})
    return json.dumps({"page_count": len(pages), "pages": pages}, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# Output schema (structured outputs)
# ---------------------------------------------------------------------------


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


# Public: the adjudicator's resolution citations are constrained by the same
# schema and validated by the same ``check_provenance`` (ADR 012).
PROVENANCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["cell", "prose"]},
        "page": {"type": "integer"},
        "table_id": _nullable({"type": "string"}),
        "row": _nullable({"type": "integer"}),
        "col": _nullable({"type": "integer"}),
        "prose_index": _nullable({"type": "integer"}),
    },
    "required": ["kind", "page", "table_id", "row", "col", "prose_index"],
    "additionalProperties": False,
}

_ATTR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "key": {"type": "string"},
        "value": {
            "anyOf": [
                {"type": "number"},
                {"type": "string"},
                {"type": "boolean"},
                {"type": "null"},
            ]
        },
    },
    "required": ["key", "value"],
    "additionalProperties": False,
}

_RECORD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # source_page is NOT here: the pipeline knows which page a table_id
        # sits on and injects it. Asking the model for something the server
        # already holds only creates a way for the run to fail (baseline run:
        # this single key cost 18 of document 05's 19 records).
        "table_id": {"type": "string"},
        "record_type": {"type": "string", "enum": [member.value for member in RecordType]},
        "jurisdiction": {"type": "string"},
        "attribute_key": _nullable({"type": "string"}),
        "filing_status": _nullable(
            {"type": "string", "enum": [member.value for member in FilingStatus]}
        ),
        "taxpayer_class": _nullable({"type": "string"}),
        "tax_year": _nullable({"type": "integer"}),
        "effective_from": _nullable({"type": "string"}),
        "effective_to": _nullable({"type": "string"}),
        "lifecycle_status": {
            "type": "string",
            "enum": [member.value for member in LifecycleStatus],
        },
        "lower_bound": _nullable({"type": "integer"}),
        "upper_bound": _nullable({"type": "integer"}),
        "rate": _nullable({"type": "number"}),
        "amount": _nullable({"type": "number"}),
        "currency": _nullable({"type": "string"}),
        "confidence": {"type": "number"},
        "source_table_label": {"type": "string"},
        "extra_attrs": {"type": "array", "items": _ATTR_SCHEMA},
        "convention_derived": {"type": "array", "items": {"type": "string"}},
        "provenance": {"type": "array", "items": PROVENANCE_SCHEMA},
    },
    # The true semantic core: what only a reader of THIS document can supply.
    # Everything omitted below is either derived server-side (table_id, and
    # source_page which is not asked for at all) or safely absent-as-null
    # (effective_from/to, extra_attrs, convention_derived). Shrunk from 20
    # keys after the baseline run, where required-but-derivable fields were
    # the single largest cause of record loss.
    "required": [
        "record_type",
        "jurisdiction",
        "attribute_key",
        "filing_status",
        "taxpayer_class",
        "tax_year",
        "lifecycle_status",
        "lower_bound",
        "upper_bound",
        "rate",
        "amount",
        "currency",
        "confidence",
        "source_table_label",
        "provenance",
    ],
    "additionalProperties": False,
}

_ISSUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source_page": {"type": "integer"},
        "table_id": _nullable({"type": "string"}),
        "row_index": _nullable({"type": "integer"}),
        "col_index": _nullable({"type": "integer"}),
        "raw_value": _nullable({"type": "string"}),
        "reason": {"type": "string"},
    },
    "required": ["source_page", "table_id", "row_index", "col_index", "raw_value", "reason"],
    "additionalProperties": False,
}

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "records": {"type": "array", "items": _RECORD_SCHEMA},
        "issues": {"type": "array", "items": _ISSUE_SCHEMA},
    },
    "required": ["records", "issues"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# System prompt — the canonical conventions
# ---------------------------------------------------------------------------

_MAPPER_ROLE = """\
You are the semantic schema mapper inside a tax-table ingestion pipeline.

Your input is a machine-extracted view of one PDF document: cell grids
(verbatim, row by row) and prose blocks (headings, body text, footnotes),
with page numbers, table ids, and per-page prose indexes. Your output is a
set of canonical records plus a set of issues, in the enforced JSON schema.
You decide what each cell MEANS; you never re-read the original pixels and
you never invent a value.

## Hard rules

1. Every value you emit must be read from a specific cell or prose block of
   this input, and every record must list its sources in "provenance"
   (kind "cell" with table_id/row/col, or kind "prose" with page and
   prose_index; row and col are 0-based indexes into "rows"). At least one
   source per record.
2. This corpus is synthetic. Do not supply or "correct" numbers from your
   knowledge of real-world tax law: a plausible real figure that is not
   printed in this input is wrong by definition. Transcribe, convert units
   as the conventions below require, and nothing more.
3. Never guess and never drop. A cell you cannot confidently interpret —
   unreadable, ambiguous, contradictory — becomes an entry in "issues" with
   its coordinates and a reason. Every data cell must either contribute to
   some record or be named in an issue.
4. A dash standing alone in a value cell (—, –, -) means the jurisdiction
   imposes no such value: emit the record with null in that slot. Null is a
   fact; it is not 0 and it is not "unreadable".
5. tax_year comes ONLY from statements in the document text ("applies to
   tax year 2026", "effective for taxable years beginning after ..."). A
   document ID or bulletin number is NEVER evidence of the tax year —
   bulletins are issued in the year before the year they govern. If a
   prose sentence states the year, every record of the document inherits
   it unless a column header says otherwise.
6. Header rows, repeated continuation headers, "(continued)" captions,
   column captions, and section titles are structure, not data: emit no
   records for them and no issues about them.
7. A footnote or body sentence can be the only place a fact exists (a rate,
   a rule, a supersession notice). Map those facts as records too, with
   prose provenance.

"""

#: The canonical target-schema law, shared verbatim with the RecordVerifier's
#: prompt (ADR 012): the two roles must judge by the same conventions, or a
#: verifier "dispute" could be a mere prompt divergence. Sharing the output
#: contract does not breach the roles' independence — what stays separate is
#: each role's *reading of the document*, never the definition of the target.
CANONICAL_CONVENTIONS = """\
## Canonical value conventions

- rate: a decimal fraction (0.22 means 22%). A cell printed "22%" or a
  column headed "Rate (%)" holding "22" both map to 0.22 in the "rate"
  slot. EXCEPTION: attribute keys ending in "_pct" keep the percentage
  number exactly as printed (3.25 stays 3.25).
- Bracket bounds: inclusive whole-currency integers. Strip currency signs
  and thousands separators. "$0 – $9,000", "0 to 9,000", "up to 9,000" all
  mean lower_bound 0, upper_bound 9000. "and over", "or more", "No limit"
  as an upper end means upper_bound null (open-ended). A bracket printed
  "$9,001 – $38,000" has lower_bound 9001: transcribe, never re-derive.
- amount: whole integers unless the source prints cents. Amounts without a
  currency sign are still amounts.
- currency: the ISO 4217 code of the document's denomination. A United
  States tax document denominates in USD even where signs are omitted.
- jurisdiction: "US" for United States federal documents; for sub-national
  rows use the jurisdiction's name exactly as printed (e.g. "Alabama").
- filing_status: map the printed label to one of single,
  married_filing_jointly, married_filing_separately, head_of_household,
  qualifying_surviving_spouse. A schedule that applies to a non-individual
  taxpayer class (e.g. estates and trusts) uses filing_status null and
  taxpayer_class set to the lowercase snake_case of the printed class name.
- lifecycle_status: "superseded" ONLY when the document itself states it
  has been superseded or replaced; otherwise "active". Every record of a
  superseded document is superseded.
- effective_from / effective_to: ISO dates, only when the document states
  them ("effective January 1, 2026" -> effective_from 2026-01-01).
- confidence: your certainty in this record's mapping, 0 to 1. Use values
  below 0.7 only when you have a concrete doubt (and say why in an issue if
  the doubt concerns a specific cell).

## Record shapes

record_type and its attribute_key sub-discriminator (attribute_key is null
unless listed; its value is the lowercase snake_case slug of the label the
document prints):

- ordinary_income_bracket: one record per (bracket row x filing-status or
  taxpayer-class column) of an income-tax rate schedule. A wide matrix with
  one rate column and several filing-status columns yields one record per
  filing status per row: same rate, that column's bounds.
- preferential_gain_bracket: same shape for preferential (e.g. capital
  gain) rate schedules.
- special_gain_rate: a flat rate for a special asset or gain category;
  attribute_key = the category slug.
- standard_deduction: amount per filing status.
- additional_standard_deduction: attribute_key = the qualifying condition
  slug (e.g. age or blindness conditions, as printed).
- dependent_deduction_rule: a rule stated in prose. amount = the base
  amount if the sentence states one; put the sentence verbatim in an extra
  attr "rule".
- sales_tax_rate: one record per jurisdiction row. Column values go to
  extra attrs named from the printed headers in snake_case with "_pct" for
  percentage columns (e.g. state_rate_pct, avg_local_rate_pct,
  combined_rate_pct), keeping percentages as printed and null for dashes.
  The typed "rate" slot carries the combined (total) rate converted to a
  decimal fraction, or null if the combined value is null. Map a derived
  or computed column like any other column — downstream validators check
  its arithmetic; you do not. When the document marks a jurisdiction as
  imposing no state sales tax (a dash plus an explanatory note), also emit
  the boolean extra attr "imposes_state_sales_tax": false on that record,
  with provenance to the note.
- employment_tax_rate: attribute_key = the tax component slug (as printed,
  e.g. social security / medicare variants). A single-rate row puts the
  fraction in "rate"; parallel columns for different payer sides become
  extra attrs named from the printed headers (snake_case, "_pct" if
  printed as percentages; fractions in a plain "_rate" attr otherwise).
- wage_base: attribute_key = the item slug; amount = the wage base. "No
  limit" means amount null.
- surtax_threshold: attribute_key = the surtax name slug; amount = the
  threshold; rate = the surtax rate if the document states one at that row
  or in a footnote (footnote provenance then).
- withholding_allowance: attribute_key = the payroll period slug (weekly,
  biweekly, ...); amount = the allowance.

## Fixed attribute_key vocabulary

attribute_key is NEVER free-form. Where a record_type takes one, use exactly
one of the keys listed below — they are the snake_case slugs of the labels
these documents print, fixed so that the same row yields the same key on
every run. (A free-form slug drifted between runs of the same document:
"per_qualifying_condition_rule" one run, "age_and_blindness_rule" the next,
which breaks natural-key matching and idempotency alike.)

- additional_standard_deduction (condition):
    unmarried_single_or_head_of_household, married_per_qualifying_spouse
- employment_tax_rate (component):
    social_security_oasdi, medicare_hi, total, futa_effective_rate
- special_gain_rate (category):
    unrecaptured_section_1250_gain,
    collectibles_and_certain_small_business_stock, short_term_capital_gain
- surtax_threshold (surtax):
    additional_medicare_tax, net_investment_income_tax
- wage_base (item):
    social_security_wage_base, medicare_wage_base, futa_wage_base_federal
- withholding_allowance (payroll_period):
    weekly, biweekly, semimonthly, monthly, quarterly, annually

Every other record_type uses attribute_key null. If a document presents a
labelled row whose slug is not on this list, still emit the record with the
slug you derive AND raise an issue naming it: an unlisted label is a gap in
this vocabulary, and it must be visible rather than silently absorbed.

## Two tax years in one row — settled, do not deviate

Some tables print the current year next to the prior year for comparison.
**One record per item, never one per (item, year).** The current tax year
(per rule 5) is the record's tax_year; the prior-year value rides on the SAME
record as an extra attr named "prior_year_amount" (amount-shaped values),
"prior_year_rate" (rate fractions), or "prior_year_<header>_pct" (percentage
columns), and a "change"/difference column rides as "change". Do not emit
separate records for the comparison year, and never drop that column: both
years must survive, in one record.

## Do not emit the same fact twice

A rate or threshold stated in prose that QUALIFIES the rows of a table
belongs on those rows' records (with prose provenance alongside the cell
citations). Do not additionally emit a standalone record for it. One printed
fact is one record; a second record for the same fact is a duplicate that a
reviewer has to reconcile.

## Convention-derived fields

A few fields come from these conventions rather than from anything the
document prints — most often "jurisdiction" on a federal document that names
no jurisdiction, and "currency" where no sign or code appears. When you
assert such a field with no textual anchor anywhere in the document, list its
name in "convention_derived" (e.g. ["jurisdiction"]). Never manufacture a
provenance citation for it: a convention is a legitimate source, but it must
not be dressed as a citation to a cell that does not say it.

## Table identity

- table_id: copy the extraction table_id verbatim ("p1_t0"). A record read
  purely from prose uses "p<page>_prose<index>" of its primary block. If you
  omit it, it is taken from your own provenance citations; do not omit
  provenance.
- source_page is not yours to supply: the pipeline derives it from table_id.
- source_table_label: the designator the DOCUMENT prints for the table or
  section the record came from, lowercased with non-alphanumeric runs
  collapsed to "_": "Table 1" -> "table_1", "Section 3." -> "section_3",
  "Table A. Rates (continued)" -> "table_a". Records read from a footnote
  use "footnote". If a table has no printed designator, slug its caption.

## Extra attrs

Extra attrs carry everything type-specific the document states that has no
typed slot: *_pct columns, payer-side rate columns, prior-year values,
prose rules, boolean facts (e.g. whether a state imposes a tax). Keys are
lowercase snake_case derived from the printed labels. Do not add
commentary attrs; "provenance", "source_table_label" and
"convention_derived" are supplied in their own fields, not in extra_attrs.

extra_attrs is an ARRAY of {"key": ..., "value": ...} objects, not an
object. An empty set of extra attrs is [] or omitted.

## Output discipline

Return the JSON object and nothing else: no prose before it, no commentary
after it, no explanation of your reasoning. Anything you would want to say
about a value belongs in "issues", which is part of the object. Numbers are
JSON numbers, never quoted strings.
"""

SYSTEM_PROMPT = _MAPPER_ROLE + CANONICAL_CONVENTIONS

_USER_INSTRUCTION = (
    "Map the following extracted document to canonical records per the "
    "conventions. Account for every data cell: record or issue.\n\n"
)


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------


def _as_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} is a boolean, expected an integer")
    value = adapt_numeric(value, role=conformance.MAPPER)
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        raise ValueError(f"{field} is not an integer: {value}")
    raise ValueError(f"{field} has unexpected type {type(value).__name__}")


def _as_decimal(value: object, field: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} is a boolean, expected a number")
    # Closed-list adaptation: a quoted number becomes a Decimal and is
    # counted. A non-numeric string falls through unchanged and is rejected
    # below, because "Ordinary rates" in a rate slot is a semantic error.
    value = adapt_numeric(value, role=conformance.MAPPER)
    if isinstance(value, Decimal | int):
        return Decimal(value)
    raise ValueError(f"{field} has unexpected type {type(value).__name__}")


def _as_date(value: object, field: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValueError(f"{field} has unexpected type {type(value).__name__}")


def check_provenance(refs: list[Any], extracted: ExtractedDocument) -> None:
    """Structural half of the traceability contract: every reference must
    name a cell or prose block that exists in the extracted document.

    Public because it is the shared citation law of the semantic layer: the
    mapper's provenance, and the adjudicator's resolution citations, are
    validated by exactly the same rules (ADR 012)."""
    if not refs:
        raise ValueError("record has no provenance: every value must trace to a source")
    tables = {table.table_id: table for table in extracted.tables}
    prose_by_page = {page.page_number: page.prose for page in extracted.pages}
    for ref in refs:
        kind = ref.get("kind")
        if kind == "cell":
            table = tables.get(ref.get("table_id"))
            if table is None:
                raise ValueError(f"provenance names unknown table {ref.get('table_id')!r}")
            row = _as_int(ref.get("row"), "provenance.row")
            col = _as_int(ref.get("col"), "provenance.col")
            if row is None or col is None or not (0 <= row < len(table.rows)):
                raise ValueError(f"provenance row {row} outside table {table.table_id}")
            if not (0 <= col < max(table.column_count, len(table.rows[row]))):
                raise ValueError(f"provenance col {col} outside table {table.table_id}")
        elif kind == "prose":
            blocks = prose_by_page.get(_as_int(ref.get("page"), "provenance.page") or -1, [])
            index = _as_int(ref.get("prose_index"), "provenance.prose_index")
            if index is None or not (0 <= index < len(blocks)):
                raise ValueError(f"provenance prose_index {index} outside page {ref.get('page')}")
        else:
            raise ValueError(f"provenance kind {kind!r} is not 'cell' or 'prose'")


def _extraction_floor(table_id: str, extracted: ExtractedDocument) -> Decimal:
    """The extraction-layer confidence beneath this record's source: mapping
    certainty can never exceed the certainty of what was read off the page."""
    for table in extracted.tables:
        if table.table_id == table_id:
            return table.confidence
    for page in extracted.pages:
        for index, block in enumerate(page.prose):
            if table_id == f"p{page.page_number}_prose{index}":
                return block.confidence
    return Decimal(1)


def _table_id_from_provenance(refs: list[Any]) -> str | None:
    """The table or prose block this record was read from, taken from its own
    citations.

    The model names the source inside every provenance entry; asking it to
    repeat that at the top level was a second chance to omit it, and on the
    baseline run it omitted it on all 32 records of document 01. Cell refs win
    over prose refs: a record citing both was read from the table.
    """
    for ref in refs:
        if isinstance(ref, Mapping) and ref.get("kind") == "cell":
            table_id = ref.get("table_id")
            if isinstance(table_id, str) and table_id:
                return table_id
    for ref in refs:
        if isinstance(ref, Mapping) and ref.get("kind") == "prose":
            page = ref.get("page")
            index = ref.get("prose_index")
            if isinstance(page, int) and isinstance(index, int):
                return f"p{page}_prose{index}"
    return None


def _source_page_for(table_id: str, extracted: ExtractedDocument) -> int:
    """The page a table_id sits on, from the extraction — never from the model.

    Authoritative rather than asserted: the extractor assigned the id and
    knows its page. The ``p<n>_`` prefix is a fallback for a prose id, and 1 a
    final one, so this can degrade but never raise.
    """
    for table in extracted.tables:
        if table.table_id == table_id:
            return table.page_number
    match = re.match(r"p(\d+)_", table_id)
    if match is not None:
        return int(match.group(1))
    return 1


def _build_record(raw: Mapping[str, Any], extracted: ExtractedDocument) -> CanonicalRecord:
    provenance = list(raw.get("provenance") or [])
    check_provenance(provenance, extracted)

    record_type = RecordType(raw["record_type"])
    attribute_key = None if raw.get("attribute_key") is None else str(raw["attribute_key"])

    attrs: dict[str, Any] = {}
    for pair in adapt_extra_attrs(raw.get("extra_attrs"), role=conformance.MAPPER):
        attrs[str(pair["key"])] = pair["value"]
    attrs["source_table_label"] = str(raw["source_table_label"])
    attrs["provenance"] = provenance
    # Fields asserted from the conventions rather than from anything printed
    # (most often `jurisdiction` on a document that names none). Declared, so
    # a reader can tell an inference from a citation (ADR 015).
    declared = raw.get("convention_derived")
    if isinstance(declared, list) and declared:
        attrs["convention_derived"] = sorted({str(name) for name in declared})
    # Mirror the sub-discriminator into the attrs tail under its per-type
    # field name, overriding any model-supplied spelling: identity and tail
    # must agree, and both derive from the same source cell.
    mirror_name = ATTRIBUTE_KEY_FIELD.get(record_type)
    if mirror_name is not None and attribute_key is not None:
        attrs[mirror_name] = attribute_key

    supplied = raw.get("table_id")
    table_id = (
        str(supplied)
        if isinstance(supplied, str) and supplied
        else _table_id_from_provenance(provenance)
    )
    if table_id is None:
        raise ValueError("record names no table_id and its provenance identifies no source")
    confidence = _as_decimal(raw["confidence"], "confidence")
    if confidence is None:
        raise ValueError("confidence is required")
    confidence = min(confidence, _extraction_floor(table_id, extracted))

    filing_status = raw.get("filing_status")
    return CanonicalRecord(
        # Injected server-side from the extraction, never asked of the model.
        source_page=_source_page_for(table_id, extracted),
        table_id=table_id,
        record_type=record_type,
        jurisdiction=str(raw["jurisdiction"]),
        attribute_key=attribute_key,
        filing_status=None if filing_status is None else FilingStatus(filing_status),
        taxpayer_class=(None if raw.get("taxpayer_class") is None else str(raw["taxpayer_class"])),
        tax_year=_as_int(raw.get("tax_year"), "tax_year"),
        effective_from=_as_date(raw.get("effective_from"), "effective_from"),
        effective_to=_as_date(raw.get("effective_to"), "effective_to"),
        lifecycle_status=LifecycleStatus(raw["lifecycle_status"]),
        lower_bound=_as_int(raw.get("lower_bound"), "lower_bound"),
        upper_bound=_as_int(raw.get("upper_bound"), "upper_bound"),
        rate=_as_decimal(raw.get("rate"), "rate"),
        amount=_as_decimal(raw.get("amount"), "amount"),
        currency=None if raw.get("currency") is None else str(raw["currency"]),
        attrs=attrs,
        confidence=confidence,
        review_status=ReviewStatus.CLEAN,
    )


def _issue_from_failure(raw: Mapping[str, Any], reason: str) -> MappingIssue:
    source_page = raw.get("source_page")
    page = source_page if isinstance(source_page, int) and source_page >= 1 else 1
    table_id = raw.get("table_id")
    return MappingIssue(
        source_page=page,
        table_id=table_id if isinstance(table_id, str) else None,
        row_index=None,
        col_index=None,
        raw_value=json.dumps(dict(raw), default=str, ensure_ascii=False)[:2000],
        reason=reason,
    )


def parse_mapping_payload(text: str, *, extracted: ExtractedDocument) -> MappingResult:
    """Parse the model's JSON into a MappingResult, Decimal-safe.

    A record that fails canonical validation is converted to an issue with
    the model's raw proposal as ``raw_value`` — reviewable, never dropped.
    """
    try:
        payload = loads_fence_tolerant(text, role=conformance.MAPPER, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise MapperError(f"mapping response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or "records" not in payload or "issues" not in payload:
        raise MapperError("mapping response JSON lacks the records/issues envelope")

    records: list[CanonicalRecord] = []
    issues: list[MappingIssue] = []
    conformance.LEDGER.record_items(conformance.MAPPER, len(payload["records"]))
    for raw in payload["records"]:
        try:
            records.append(_build_record(raw, extracted))
        except (
            ValidationError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            InvalidOperation,
        ) as exc:
            # The envelope held and this item did not: a contract miss at the
            # item level, which is exactly what the conformance rate measures.
            conformance.LEDGER.record_malformed_item(
                conformance.MAPPER, f"unmappable record: {type(exc).__name__}"
            )
            issues.append(_issue_from_failure(raw, f"unmappable record: {exc}"))
    issues.extend(_sanitize_issue(raw_issue) for raw_issue in payload["issues"])
    return MappingResult(records=records, issues=issues)


def _sanitize_issue(raw: Any) -> MappingIssue:
    """A model-emitted issue with out-of-range coordinates is degraded, not
    fatal: the reason survives, the bad coordinates do not — one malformed
    issue must never abort a document run (anti-goal #8 both ways)."""

    def _coord(value: object) -> int | None:
        return (
            value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        )

    try:
        page = raw.get("source_page")
        table_id = raw.get("table_id")
        raw_value = raw.get("raw_value")
        reason = raw.get("reason")
        return MappingIssue(
            source_page=page if isinstance(page, int) and page >= 1 else 1,
            table_id=table_id if isinstance(table_id, str) and table_id else None,
            row_index=_coord(raw.get("row_index")),
            col_index=_coord(raw.get("col_index")),
            raw_value=raw_value
            if raw_value is None or isinstance(raw_value, str)
            else str(raw_value),
            reason=reason if isinstance(reason, str) and reason else "unspecified",
        )
    except (ValidationError, ValueError, TypeError, AttributeError) as exc:
        conformance.LEDGER.record_malformed_item(
            conformance.MAPPER, f"malformed issue object: {type(exc).__name__}"
        )
        return MappingIssue(
            source_page=1,
            raw_value=json.dumps(raw, default=str, ensure_ascii=False)[:2000],
            reason=f"malformed issue from mapper: {exc}",
        )


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class AnthropicSchemaMapper:
    """SchemaMapper over the Anthropic Messages API (or any endpoint that
    speaks it, such as the Vercel AI Gateway)."""

    def __init__(self, config: MapperConfig, *, client: Any | None = None) -> None:
        # ``client`` is injectable for tests; anything exposing
        # ``messages.stream(**kwargs)`` with a ``get_final_message()``
        # context manager qualifies.
        self._config = config
        self._client = client if client is not None else self._build_client(config)

    @staticmethod
    def _build_client(config: MapperConfig) -> anthropic.Anthropic:
        return anthropic.Anthropic(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=_REQUEST_TIMEOUT_SECONDS,
            max_retries=3,
            # Every retry the SDK absorbs is one more request through this
            # transport, which is the only place they are visible.
            http_client=conformance.instrumented_http_client(
                conformance.MAPPER, timeout=_REQUEST_TIMEOUT_SECONDS
            ),
        )

    def map_document(self, extracted: ExtractedDocument) -> MappingResult:
        return with_bounded_retries(
            lambda: self._map_document(extracted),
            role=conformance.MAPPER,
            contract_error=MapperError,
            retries=self._config.contract_retries,
            contract_backoff=self._config.contract_retry_seconds,
            transport_backoff=self._config.transport_retry_seconds,
        )

    def _map_document(self, extracted: ExtractedDocument) -> MappingResult:
        started = time.perf_counter()
        # Streaming keeps long generations (document 03 maps 50+ records)
        # clear of HTTP timeouts; the shared system prompt carries a cache
        # breakpoint so a five-document run pays for it once.
        with self._client.messages.stream(
            model=self._config.model,
            max_tokens=self._config.max_output_tokens,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": _USER_INSTRUCTION + serialize_document(extracted),
                }
            ],
            output_config={"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}},
        ) as stream:
            message = stream.get_final_message()

        if message.stop_reason != "end_turn":
            raise MapperError(
                f"mapping call ended with stop_reason={message.stop_reason!r}; "
                "refusing to parse a truncated or refused response"
            )
        text: str | None = None
        for block in message.content:
            candidate = getattr(block, "text", None) if block.type == "text" else None
            if candidate:
                text = candidate
                break
        if text is None:
            raise MapperError("mapping response contains no text block")

        result = parse_mapping_payload(text, extracted=extracted)
        usage = message.usage
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        usd = (
            Decimal(input_tokens) * self._config.usd_per_mtok_in
            + Decimal(cache_write) * self._config.usd_per_mtok_in * self._config.cache_write_factor
            + Decimal(cache_read) * self._config.usd_per_mtok_in * self._config.cache_read_factor
            + Decimal(output_tokens) * self._config.usd_per_mtok_out
        ) / _MTOK
        cost = MappingCost(
            engine=self._config.model,
            api_calls=1,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_write_tokens=cache_write,
            cache_read_tokens=cache_read,
            usd=usd,
            wall_seconds=time.perf_counter() - started,
        )
        return MappingResult(records=result.records, issues=result.issues, cost=cost)
