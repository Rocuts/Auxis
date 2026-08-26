# Development log

Chronological record of what was tried, what failed, and what was chosen — as
required by the brief ("documentation of development steps and tool choices").
This project is built with heavy, deliberate AI assistance (Claude Code); for an
AI Engineer role that is signal, not something to hide. Orchestration choices are
logged here alongside engineering ones.

## 2026-08-25 — Project setup

- **Fixtures committed first.** The brief supplied no documents, so the five
  input PDFs and `ground_truth.json` are self-authored, produced by the committed
  generator scripts (`fixtures/gen_fixtures.py`, `gen_groundtruth.py`,
  `scanify.py`). Each document is designed to break a different naive extraction
  assumption; see CLAUDE.md "The input documents".
- **Environment verified before writing code.** Docker 29.7.2 (local Postgres for
  tests), uv 0.10.10, Node 25, Vercel CLI 59.5.0 linked to project `auxis` with
  the Neon Marketplace integration attached.
- **Finding: Vercel Queues is not available on this account's scope.** The
  dashboard shows no Queues product. Decision: the Vercel `JobRunner` adapter
  will be the cron-sweep fallback anticipated in CLAUDE.md — a jobs-table sweep
  driven by a `vercel.json` cron at minute granularity. The latency trade-off
  will be recorded in the JobRunner ADR at Phase 3.5.
- **Finding: the Neon integration creates its env vars as Sensitive**, so
  `vercel env pull` returns `[SENSITIVE]` placeholders instead of values. Runtime
  functions receive real values; local runs against Neon (the Phase 1 gate) need
  the connection string copied from the Neon console into `.env`.
- **Tool discipline encoded in the repo.** `.claude/settings.json` allowlists the
  dev toolchain (uv, pytest, ruff, mypy, docker, psql, git, curl, node, make),
  forces a prompt on every `vercel` command, and hard-denies
  `vercel deploy --prod` / `promote` / `rollback` and `cdk deploy` / `bootstrap` —
  anti-goals #5 and #9 enforced at the harness level, not just documented.
- **Orchestration plan.** Sequential single-agent work by default. Multi-agent
  fan-out is reserved for the three phases where it genuinely helps: Phase 2
  (one agent per fixture PDF, adversarial verification of extracted records),
  Phase 4 (audit of the synthesized CloudFormation template), Phase 5 (final
  review against the evaluation criteria).

## 2026-08-25 — Phase 0: scaffold

- `uv` project, Python 3.12, src layout (`src/tax_tables/`), hatchling build so
  the package installs editable and imports cleanly from tests.
- ruff (lint + format) with a broadened rule set, mypy `strict = true`, pytest.
  One smoke test so `make check` exercises the whole toolchain on the empty
  project rather than trivially passing with zero tests collected.
- `make check` = lint + typecheck + test; the same entrypoint runs locally and in
  CI (GitHub Actions, `astral-sh/setup-uv` with caching) so the Phase 0 gate is
  one command in both places.
- `.env.example` documents every required variable per anti-goal #10; `.env` is
  gitignored and never enters the repo.
- **Gate result:** `make check` clean locally on the first run, and green in CI
  on the first push (run 32911320432, 16s). Repo: github.com/Rocuts/Auxis.

## 2026-08-25 — Phase 1: DDL, validated against current practice

Before writing the schema, a research agent verified the stack assumptions
against August-2026 documentation. Material corrections it produced:

- **Neon defaults to Postgres 18** for projects created after 2026-06-05, and
  the major version is immutable after creation. The local image is therefore
  pinned to `postgres:18`, not 17; migrations and the gate probes were verified
  against 18.6. To confirm against the real branch once `.env` exists:
  `SHOW server_version`. Caveat for docker-compose: the official `postgres:18`
  image moved `PGDATA` to a version-specific path (`/var/lib/postgresql/18/docker`),
  so 17-era volume mounts silently initialize an empty database.
- **`prepare_threshold=None` is obsolete.** psycopg >= 3.2 supports protocol-level
  prepared statements through PgBouncer >= 1.22 (Neon runs 1.22 with
  `max_prepared_statements=1000`) when bundled libpq >= 17. The repository
  adapter will gate on `psycopg.capabilities.has_send_close_prepared()` instead
  of blanket-disabling prepared statements.
- **`int8range` over `numrange` is a choice, not a necessity.** numrange would
  also support unbounded tops, but int8range's canonicalization (inclusive
  integer bounds normalize to half-open) is what makes adjacency and gap
  detection exact. Bracket bounds in the fixtures are whole-currency integers,
  so the discrete type fits.
- **PG18's `UNIQUE ... WITHOUT OVERLAPS` evaluated and rejected** for the
  polymorphic table: it cannot be partial (`WHERE bracket IS NOT NULL`) and
  forces sentinel values into discriminators the canonical schema keeps NULL.
  The hand-written GiST exclusion constraint with COALESCE stays; rationale
  recorded in `migrations/0003_records.sql`.
- **Migrations must run over the direct (unpooled) endpoint** — Neon lists
  schema migrations first among operations that need it — with
  `pg_advisory_lock` for serialization, generous `connect_timeout` (scale-to-zero
  resume), and `lock_timeout` before DDL.
- Noted for Phase 3: FastAPI 0.132+ enforces strict `Content-Type` on JSON
  bodies by default; Pydantic v2 (2.13.x) is current, v3 does not exist.

DDL verified live before review: all four migrations apply cleanly on
Postgres 18.6, overlap insert rejected by `no_overlapping_brackets`, adjacent +
open-ended top bracket accepted, negative rate accepted, and the NULL
`taxpayer_class` trap (document 05) confirmed closed by the COALESCE chain.

## 2026-08-25 — Phase 1: review findings and implementation

The DDL was human-reviewed line by line and approved with two design
conditions, both now encoded and tested:

- **Cross-document conflict policy.** The natural key deliberately excludes
  `document_id` (cross-document dedup), which means a plain upsert could let
  one document silently overwrite another's value — the neighbor of
  anti-goal #8. The adapter's upsert carries
  `WHERE records.document_id = EXCLUDED.document_id`: same document refreshes
  the row (updated_at moves); a different document is refused, and the incoming
  record goes to the review queue with reason
  `cross_document_natural_key_conflict`. Tested both ways.
- **Supersession rule, stated explicitly:** lifecycle_status is declared by
  document content (document 05 self-declares superseded; the mapper reads it
  from the body) — never inferred from arrival order, and the repository never
  promotes/demotes rows as an insert side effect. Corollary: ingestion is
  commutative, proven by a repository-level test that ingests an active set
  and a superseded set in both orders and asserts identical final state. The
  full five-fixture commutativity test lands with the Phase 2 pipeline.

Minor review fixes: `effective_window` CHECK, `updated_at` audit column,
COALESCE(filing_status) re-commented as deliberate defense-in-depth (only
taxpayer_class strictly needs it), and a note that the exclusion constraint's
GiST index serves `GET /records/resolve` only when queries use the same
COALESCE expressions. Migration homes: btree_gist in 0001, documents/jobs in
0002, records in 0003, review_queue + gap view in 0004.

Implementation: Pydantic v2 canonical model (strict, frozen, shape-validated),
plain-SQL migration runner (direct endpoint, advisory lock, per-file
transaction, `-- migrate:no-transaction` escape hatch), psycopg adapter with
capability-gated prepared statements and per-record savepoints, docker-compose
on postgres:18 (parent-dir volume mount per the PG18 PGDATA change), CI gains
a Postgres 18 service container. Gate: `make check` green — ruff, mypy strict,
11 tests including the three gate properties, conflict policy, and
commutativity. Pending: the same migrations against the Neon branch (needs
DATABASE_URL in .env, user-supplied) to close the Neon half of the gate.

**Phase 1 gate closed against Neon (2026-08-25).** Direct endpoint: cold
connect 1.40s (scale-to-zero resume — the latency the Serverless v2 ADR is
about), `server_version` **18.6**, confirming the validator's PG18 finding and
the local `postgres:18` pin. All four migrations applied via the runner over
the direct endpoint; `btree_gist` v1.8 installed with no friction. The three
gate probes were re-proven ON the Neon branch inside a rolled-back
transaction (overlap rejected, open-ended top accepted, negative rate
accepted; zero rows left behind). Pooled endpoint: connect 0.55s,
`psycopg.capabilities.has_send_close_prepared()` = True — the modern
prepared-statements path is active, and 8 repeated parameterized queries
(crossing prepare_threshold=5) executed through PgBouncer without error.
No `prepare_threshold=None` workaround needed, as researched.

**CI failure worth recording:** the Phase 1 push failed lint in CI while local
`make check` was green. Creating `tests/__init__.py` mid-session changed how
ruff's isort classifies `tests.*` imports (third-party -> first-party), but
ruff's local cache skipped re-linting unchanged files under the new
classification — a false green. Fix: imports re-sorted with `--no-cache`, and
`known-first-party = ["tax_tables", "tests"]` pinned in pyproject so
classification is declared, not inferred. Lesson: any config change that
alters lint semantics deserves a `--no-cache` run before push.
