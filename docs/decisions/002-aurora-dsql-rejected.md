# ADR 002 — Aurora DSQL rejected

**Status:** accepted (rejection) · **Date:** 2026-08-25 · **Phase:** 1

## Context

Aurora DSQL is the obvious modern candidate for a serverless, PostgreSQL-
compatible, scale-to-zero database: no instance to size, no connection
exhaustion under fan-out, no cost when idle. For a service whose stated
bottleneck is *connections under fan-out*, that is exactly the right shape.

It was evaluated seriously and rejected on feature support, not on taste.

## Decision

**Do not use Aurora DSQL.** Use PostgreSQL ([ADR 001](001-postgresql-engine.md)).

## Why — from the published compatibility documentation

The rejection does not rest on general "it's a subset" hand-waving. Two
specific things this design is built on are absent from AWS's own supported-
feature lists.

**1. There is no `EXCLUDE` constraint.** The [supported `CREATE TABLE`
syntax](https://docs.aws.amazon.com/aurora-dsql/latest/userguide/create-table-syntax-support.html)
enumerates the grammar Aurora DSQL accepts. Its `table_constraint` production
is, verbatim:

```
[ CONSTRAINT constraint_name ]
{ CHECK ( expression ) |
  UNIQUE [ NULLS [ NOT ] DISTINCT ] ( column_name [, ... ] ) index_parameters |
  PRIMARY KEY ( column_name [, ... ] ) index_parameters |
```

`CHECK`, `UNIQUE`, `PRIMARY KEY`. No `EXCLUDE`, and no `REFERENCES` /
`FOREIGN KEY` in either the column or table production.

**2. There are no range types.** The [supported data
types](https://docs.aws.amazon.com/aurora-dsql/latest/userguide/working-with-postgresql-compatibility-supported-data-types.html)
page states that "Aurora DSQL supports a subset of the common PostgreSQL
types" and then enumerates them: numeric, character, date/time, and
miscellaneous (`boolean`, `bytea`, `UUID`, `json`, `jsonb`). No `int8range`,
no range types at all — and no `CREATE TYPE` to define one.

Those two together are fatal *specifically here*. The `bracket int8range`
column cannot exist, and the constraint that makes overlapping brackets
unrepresentable cannot be written. The centrepiece of the data model would
degrade into application-level validation — which is precisely the thing
[ADR 001](001-postgresql-engine.md) chose Postgres to avoid.

Three further differences would each have cost something, and are recorded so
this rejection is not re-litigated on the first one someone rediscovers:

- **Referential integrity is the application's job.** AWS's migration guide is
  explicit: "For referential integrity, implement validation in your
  application layer." Every `ON DELETE CASCADE` in the schema — documents to
  blobs, jobs, records, review items — becomes hand-written cleanup.
- **One DDL statement per transaction.** The migration runner applies each
  `.sql` file atomically; several files contain multiple statements. The
  migration model would need rewriting.
- **3,000 rows per transaction**, across all DML. Not binding for a
  128-record fixture set; squarely binding for the 10,000 documents/day target
  in the README's bottleneck section, where a batch ingest would have to be
  chunked.

## When this rejection would flip

If Aurora DSQL adds range types **and** exclusion constraints, re-evaluate
immediately: the operational argument for it is genuinely strong, and the
connection-exhaustion bottleneck that RDS Proxy exists to mitigate would
simply stop existing. Nothing else about the design would have to change,
because persistence is behind the `RecordRepository` port — the migration
would be one adapter and one set of migrations, not a rewrite.

Absent range types, no amount of operational elegance compensates: the product
would be storing tax brackets in a database that cannot tell it when two of
them overlap.
