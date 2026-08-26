# ADR 001 — PostgreSQL as the database engine

**Status:** accepted · **Date:** 2026-08-25 · **Phase:** 1

## Context

The domain is tax brackets. A bracket is a *range*, and the single most
valuable integrity property in the whole system is that two active brackets in
the same chain must never overlap. Overlapping brackets are not a cosmetic
data-quality issue — they make `GET /records/resolve?amount=…` ambiguous, which
is the one endpoint that has to be unambiguous.

The service also has to run on three targets (local container, Neon, RDS)
against one codebase and one set of migrations.

## Decision

**PostgreSQL 18**, with `btree_gist`, on all three targets, driven by
`psycopg` 3.

The version is pinned to the same major everywhere — `postgres:18` in
`docker-compose.yml`, `PostgresEngineVersion.VER_18_3` in the CDK stack, and
the Neon branch — so no target can develop a dialect the others do not share.

## Why

Two Postgres features carry the design, and neither has a portable equivalent:

1. **Range types plus GiST exclusion constraints.** The bracket column is an
   `int8range`, and overlap is prevented by the database:

   ```sql
   EXCLUDE USING gist (
       jurisdiction WITH =, record_type WITH =, tax_year WITH =,
       filing_status WITH =, taxpayer_class WITH =, bracket WITH &&
   ) WHERE (bracket IS NOT NULL AND lifecycle_status = 'active')
   ```

   Mixing scalar equality with range overlap in one GiST index requires the
   `btree_gist` extension. The result is that an overlapping bracket is not
   *rejected by validation* — it is **unrepresentable**. That is a
   categorically stronger guarantee than application-level checking, because it
   survives a bug, a concurrent writer, and a manual `INSERT`.

2. **`UNIQUE NULLS NOT DISTINCT`.** The records' natural key includes columns
   that are legitimately null (`filing_status` is null for a sales-tax rate).
   Standard SQL `UNIQUE` treats every null as distinct, which would let
   duplicates through exactly where the key matters most. Postgres 15+ lets the
   constraint say what is meant.

Supporting reasons: `int8range` with a null upper bound expresses the
open-ended top bracket (`and over`) natively; `numeric` gives exact decimal
rates without float error, and permits the legitimately negative local rate in
document 03; `jsonb` carries the type-specific attribute tail; and one driver
(`psycopg` 3) speaks to all three targets unchanged.

## Alternatives

- **Aurora DSQL** — [ADR 002](002-aurora-dsql-rejected.md). Its supported
  `CREATE TABLE` grammar has no `EXCLUDE` clause and its supported-type list
  has no range types.
- **MySQL** — no range types, no exclusion constraints, no partial indexes.
  The centrepiece would become application code.
- **SQLite** — no concurrent writers, and the fan-out is the point.
- **A document store** — the schema *is* the deliverable here; discarding
  constraints to gain schema flexibility inverts the requirement.

## Consequences

`CREATE EXTENSION btree_gist` is a hard prerequisite on every target and is
part of the Phase 1 gate — including on the Neon branch, where it is supported.
Migrations are plain versioned `.sql` files with a small runner, because the
DDL is itself a deliverable and should read as one.

Gap-freeness (no hole between consecutive brackets) is a cross-row aggregate
and therefore *cannot* be an exclusion constraint. It lives in a validation
step and a diagnostic view instead, and this ADR records that split so nobody
later mistakes "no overlaps" for "no gaps".
