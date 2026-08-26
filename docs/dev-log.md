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

## 2026-08-25 — Phase 2a: deterministic extraction (no API key yet)

Phase 2 split at the ANTHROPIC_API_KEY boundary: 2a builds everything
deterministic — router, pdfplumber + tesseract extractors, the extracted-grid
model, validators, review triage, and the accuracy-harness skeleton. The
SchemaMapper exists as a port only. No stub, no fabricated records: the
accuracy table does not exist until it runs against the real API in 2b.

**Probe-first.** Every design decision came from measuring the five PDFs
before writing adapter code. Findings that shaped the architecture:

- Docs 02/03 have row rects but no vertical rules; pdfplumber's default
  strategy returns 1-column grids ('Alabama 4.000 5.290 9.290 5' as one
  cell). Whole-page `text` strategy is worse (merges prose into tables).
  Chosen fix: keep the ruled bbox, rebuild columns from word x-gaps — the
  same geometry problem OCR word boxes pose, so one shared `gridbuild`
  module serves both paths.
- Doc 05 (scanned, 0 chars) defeats *every* whole-page tesseract mode: PSM 3
  drops the entire rate stub column ('Rate', '0/15/20 percent'), PSM 4 drops
  the whole Table 1 body, PSM 11 loses paragraph structure. The ruling lines
  themselves confuse segmentation. Per-cell OCR after image-side line-grid
  detection recovers every lost cell at conf 94–96. Deskew is estimated from
  the image (projection-variance search), never hardcoded from fixture
  knowledge.
- A 2026 best-practices validation agent (Opus 5, web-sourced, empirical
  against our fixtures) corrected three assumptions before they shipped:
  `lines_strict` returns zero tables on doc 03 (rect-drawn grid); mean OCR
  confidence *rewards* silent dropout (PSM 4 scored the highest mean by
  losing the hard table) — so the model pairs p10 tails with coverage counts
  and a flagged-cell cap; and pytesseract was about to ride the main
  dependency list into a Vercel bundle where its binary cannot exist — now
  an optional `ocr` extra mirrored into the dev group.

**Implementation was orchestrated:** four Opus 5 agents built the two
adapters, the validators, and the harness skeleton in parallel on disjoint
files, each handed the probe data as spec. Two agent findings mattered:

- The planned thickness filter for spurious ruling lines is provably wrong:
  bold header text projects as *thin* clusters (6px and 2px at the 0.30 ink
  threshold). Replaced with a continuity rule — a horizontal rule must have
  a contiguous ink run spanning >=20% of page width. Code was made faithful
  to reality; the structural test was not weakened.
- Loading the oracle exposed a real Phase 1 defect: docs 02 and 04 carry
  'Qualifying surviving spouse' rows, but the FilingStatus enum and the 0003
  CHECK admitted only four statuses — two records unpersistable, accuracy
  capped at 126/128 before the mapper even exists. Fixed by migration 0005
  (CHECK replaced, never edited in place) + the enum member; a guard test
  keeps the status representable. Recorded here because the harness caught
  it exactly the way the gate philosophy intends: a truthful red, not a
  negotiated green.

**Gate 2a evidence** (uv run python -m tax_tables.tools.extraction_report):
router chose deterministic_text for 01–04 and ocr for 05; grids 9x5+5x2 /
5x4+2x2 / 28x5+25x5 (51 data rows stitched) / 4x4+6x2+5x3+7x3 / 4x5+4x2;
prose+footnote blocks captured on every document (doc 02's prose-only rule,
doc 03's unit sentence and rebate note, doc 05's SUPERSEDED banner and the
NOTE block carrying the 3.8% rate that exists nowhere else); docs 01–04 at
$0.07–0.12 wall and **$0 / 0 API calls** (the report exits nonzero if a
text-layer document ever spends money); doc 05 at 3.1s, 285 words, p10 0.95,
document confidence 0.92, also $0 locally. `make check`: 172 passed, 1
deliberate skip (the 2b accuracy run), ruff + mypy strict clean. An
adversarial review workflow (find-then-refute) ran over the full 2a diff
before gate close; its confirmed findings and fixes are recorded below.

**Adversarial review outcome (same day).** Four Opus 5 finders (correctness,
anti-goals, robustness/contracts, test honesty) produced 35 findings over
the 2a diff; independent refute-by-default verifiers killed 28 and
confirmed 7, several with runnable probes. All 7 fixed and pinned by tests:

1. *Router (critical).* A scanned page carrying a small text overlay (a
   60-char Bates/records stamp) cleared the 50-char threshold, routed to
   pdfplumber, and the whole document came back empty at confidence 1.0 —
   proven with a hand-built stamped-scan PDF. Fix: a page-sized-image test
   (`_scan_like`, coverage >= 0.5) that outranks the char count; both
   routing directions are now stated invariants with tests, including a
   synthetic stamped-scan case.
2. *Router (major).* /Rotate metadata sent rotated-but-upright text pages
   to the paid OCR path, violating the brief's "never send a usable text
   layer to the vision adapter". Routing now keys on upright-char count;
   sideways text layers (which pdfplumber cannot read reliably) still go to
   the pixel-licensed port.
3. *Domain (critical).* Doc 01's Estates and Trusts brackets
   (taxpayer_class-discriminated, no filing status) were unpersistable —
   the second oracle-vs-schema gap after QSS, found by the new
   constructibility guard that maps all 128 oracle entries onto
   CanonicalRecord. Fix: migration 0006 relaxes bracket_requires_chain to
   "any taxpayer discriminator"; the exclusion constraint's
   COALESCE(filing_status,'') — documented in 0003 as defense in depth —
   is now load-bearing, and a raw-SQL test proves the estate chain still
   rejects overlaps.
4. *Harness (critical).* The oracle's `table_id` is the document's printed
   label ('table_1', 'section_3'); the extractor's is provenance ('p1_t0').
   Comparing them would have mismatched all 128 records in 2b — hidden by
   synthetic tests that set both sides identically. Fix: COMPARED_AS rename
   map (label rides in attrs.source_table_label), the mapper port docstring
   now states the contract, and oracle commentary fields (note/
   extraction_note) are explicitly not compared.
5. *Validators (major).* A chain missing its lowest bracket produced zero
   findings (pairwise gap checks can't see it). New bracket_bottom rule
   flags multi-bracket chains not starting at 0; migration 0006 also
   re-anchors the bracket_gaps view (lag default 0) so the DB diagnostic
   sees the same hole.
6. *Validators (minor).* derived_sum skipped records with a partial rate
   triple — disabling itself exactly when a column had been lost. A partial
   triple now flags.
7. *Tesseract test (major).* The dropout guard (>200 words vs 285 measured)
   tolerated a 30% loss; tightened to >270, and doc 05's only tax-year
   sentence ('Tax Year 2025', one small prose block) is now pinned — losing
   it would leave tax_year inferable only from forbidden sources.

Post-fix: make check 184 passed + 1 deliberate skip, extraction report
unchanged (identical grids, costs, confidences). Gate 2a closed pending
sign-off.
