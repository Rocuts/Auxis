# ADR 005 — One polymorphic fact table over per-type tables

**Status:** accepted · **Date:** 2026-08-25 · **Phase:** 1

## Context

The five fixture documents produce **eleven** record types: ordinary income
brackets, preferential gain brackets, special gain rates, standard deductions,
additional standard deductions, dependent deduction rules, sales tax rates,
employment tax rates, wage bases, surtax thresholds, and withholding
allowances. They do not share a shape. A bracket has a range and a rate; a
wage base has an amount and no range; a dependent deduction rule is a
*condition* expressed partly in prose.

Three schema strategies were on the table: eleven tables, one JSON blob, or a
typed core with a JSONB tail.

## Decision

**One `records` table**: a typed core for everything queries and constraints
need, plus a `jsonb` `attrs` column for the type-specific tail.

The typed core carries provenance (`document_id`, `source_page`, `table_id`),
temporal validity (`tax_year`, `effective_from`, `effective_to`,
`lifecycle_status`), `jurisdiction`, a `record_type` discriminator, an
`attribute_key` sub-discriminator, three value slots (`bracket int8range`,
`rate numeric`, `amount numeric`), and `confidence` / `review_status`.

## Why not eleven tables

Every new document shape would need a migration, and the scope explicitly
covers five *deliberately heterogeneous* documents with more implied. Worse,
`GET /records` is a single endpoint with cross-cutting filters — `tax_year`,
`jurisdiction`, `effective_on`, `min_confidence`, cursor pagination. Over
eleven tables that becomes an eleven-way `UNION ALL` that has to be rewritten
every time a twelfth type appears, and stable cursor pagination across a union
of eleven differently-ordered tables is genuinely hard.

The exclusion constraint would also fragment. Bracket integrity is one rule
about one chain; expressed per-table it becomes N copies that can drift.

## Why not one blob

No constraints, no type safety, and — decisively — **no exclusion
constraint**, because `EXCLUDE USING gist` needs real typed columns to index.
The centrepiece of [ADR 001](001-postgresql-engine.md) would be lost. A blob
also cannot express `UNIQUE NULLS NOT DISTINCT` over a natural key, so
idempotency would move into application code.

## Why the hybrid is not a compromise

The split is principled rather than pragmatic: **a column is typed if a
constraint, an index, or an API filter depends on it; otherwise it rides in
`attrs`.** That rule is what keeps the JSONB tail from becoming a dumping
ground, and it is checkable — if a filter is added to `GET /records`, its
column must be promoted out of `attrs`.

What the typed core buys, concretely:

- the GiST exclusion constraint over `(jurisdiction, record_type, tax_year,
  COALESCE(filing_status, ''), COALESCE(taxpayer_class, ''), lifecycle_status,
  bracket)` — overlap is unrepresentable. The two `COALESCE` arms matter: a
  NULL discriminator is `DISTINCT` from every other NULL under `WITH =`, so
  without them two scalar chains would never be compared and the constraint
  would silently not apply to them;
- `lifecycle_status` is a **chain column, not a predicate**, so superseded
  document 05 records simply cannot collide with active 2026 ones while the
  constraint still governs both;
- a single indexed path for every documented filter;
- `numeric` rates that permit the legitimately negative local rate in
  document 03, and `NULL` rates that mean *no tax imposed* rather than zero.

## Consequences

> **ANNOTATION 2026-08-27 (adversarial review).** The paragraph below claims
> more than the schema delivers, and the correction is deflating. **Only
> `record_type` is `CHECK`-constrained. `attribute_key` ships as bare `text`
> with no CHECK in any of the eight migrations** — its vocabulary is enforced
> by prompt text alone (`adapters/anthropic_mapper.py`), and the mapper's tool
> schema types it as a free string while typing its neighbours as enums. It is
> a natural-key column, so drift there is not cosmetic: migration
> `0003_records.sql` records exactly this class of drift producing "60 rows
> where 32 are right". A model writing an unconstrained key column is the
> weakest joint in this data model, and it is stated here rather than left for
> a reader to find.

`record_type` and `attribute_key` are `CHECK`-constrained enumerations, so a
typo becomes a rejected insert rather than an orphaned row — the enumeration
is a list that grows by migration, which is the one place per-type rigidity is
worth paying for.

Queries that need a type-specific attribute reach into `attrs` and are
therefore not index-accelerated by default. That is accepted: no current
endpoint filters on one. When one does, the rule above says to promote the
column, and this ADR is the record of that rule.
