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

## 2026-08-25 — Phase 2b: the real SchemaMapper (blocked on credentials at the finish line)

**Adapter.** `AnthropicSchemaMapper` implemented test-first (20 unit tests
against an injected fake client, red before green). Design decisions:

- **Endpoint/key/model from env** — `SCHEMA_MAPPER_API_KEY/BASE_URL/MODEL`
  with `ANTHROPIC_*` fallbacks — so the direct API and the Vercel AI
  Gateway's Anthropic-compatible endpoint are the same adapter, different
  configuration. Default model `claude-opus-5`; prices env-overridable and
  feeding per-document `MappingCost` (tokens + USD incl. cache write/read
  multipliers), the semantic-layer sibling of `ExtractionCost`.
- **Structured outputs** (`output_config.format` json_schema) with an
  enum-locked record schema built from the domain enums; one streaming call
  per document (doc 03 maps 50+ records); the shared conventions prompt
  carries a cache breakpoint so a five-document run pays for it once.
- **Decimal end to end.** The model's JSON is parsed with
  `parse_float=Decimal` — the same rule the harness applies to the oracle —
  so no float ever touches a mapped value or an attr.
- **Anti-goal #8 mechanically.** A proposed record failing canonical
  validation (inverted bounds, non-integral bound, dangling provenance)
  becomes a `MappingIssue` carrying the raw proposal; the batch survives.
  Every record must name its source cells/prose blocks; the references are
  structurally checked against the extracted document and ride into
  `attrs["provenance"]`. Mapping confidence is floored by the source
  table's extraction confidence.
- The serialized grid deliberately excludes the filename: tax_year must be
  unlearnable from anything except document content (doc 02's trap).

**Conventions provenance.** The mapper prompt's conventions were authored
from CLAUDE.md, the DDL comments (attribute_key semantics), the domain
docstrings, and the oracle's `conventions` block — schema documentation the
brief directs us to read — accessed once through the licensed
`tests/accuracy` loader. No expected-record values were viewed; the
jurisdiction spelling is not documented anywhere, so the prompt derives it
from document text ("US" federal / printed name sub-national) and the
accuracy run will judge that choice honestly.

**Pipeline.** `pipeline.run_document` composes grid -> mapper -> triage ->
persist behind ports; the repository grew a public `queue_review` (mapping
issues and triage rejections land with provenance; one entry per finding).
`tools/pipeline_report` runs any set of PDFs end-to-end and dumps every
intermediate artifact as JSON; it reads only PDFs and env.
`test_end_to_end_accuracy` is now the real Phase 2 gate: PDFs -> router ->
real mapper -> comparison, printing accuracy by document, by record type,
and per-document mapper cost (`make accuracy`).

**The smoke test did its job.** Before any fan-out, one minimal call:
`ANTHROPIC_API_KEY` in `.env` turned out to be an empty line (a `sed`
redaction had made it look set). No ambient key, no `ant` profile. Probed
the Vercel AI Gateway with the project's OIDC token: authentication works
(the 403 is a model policy, not auth), but the free tier blocks every
Claude model, and topping up credits is a human decision. Asked; decision:
continue everything credential-independent, run the accuracy gate when the
key lands. `make check`: 208 passed + the credential skip, ruff + mypy
strict clean.

**Adversarial review outcome (same day).** The same find-then-refute
pattern as 2a, run over the 2b diff before any API spend: five Opus 5
finders (correctness, harness-contract fit, anti-goals, API usage, test
honesty) produced 35 findings; refute-by-default verifiers examined the 18
highest-severity and confirmed 10, which collapse to three distinct
defects — every one caught before it could burn a paid mapping run:

1. *Harness contract (critical).* The oracle asserts the sub-discriminator
   twice: as identity (attribute_key) and as a per-type attrs field
   (condition/component/category/surtax/item/payroll_period). The mapper
   wrote only the typed field, so 6 of 11 record types would have
   mismatched with `<absent>` even when mapped perfectly. Fix:
   ATTRIBUTE_KEY_FIELD now lives in domain/records.py (a naming
   convention, not oracle data) and the adapter mirrors the slug into
   attrs; a test pins the domain and harness maps equal.
2. *Persistence (major).* Mapper attrs are Decimal by construction
   (parse_float=Decimal), and psycopg's stock JSONB dumper cannot
   serialize Decimal — every _pct/prior-year record would have aborted its
   whole ingest batch with TypeError. Fix: Decimal-aware Jsonb dumps
   (default=str, exact digits), pinned by a pipeline test that round-trips
   a Decimal attr through the database.
3. *Robustness (major).* Model-emitted issues were constructed untrusted
   and unguarded — one negative row_index would have aborted the document.
   Fix: issues are sanitized (bad coordinates degrade to None, reason
   survives) and can never kill a run.

Review-tail hardening in the same commit: MapperConfig repr hides the key
(anti-goal #10), FLAG findings now reach the review queue with reasons (a
needs_review row without its why is useless to a reviewer),
pipeline_report and the accuracy gate both survive per-document mapper
failures and name them, SCHEMA_MAPPER_MAX_OUTPUT_TOKENS is honored, and
cache-read tokens count in the cost report. Post-fix: make check 214
passed + the credential skip, ruff + mypy strict clean. Phase 2b code is
gate-ready; the accuracy run itself waits on the API key (`make accuracy`
once it lands in .env).

**Pre-spend verification of the 2a tail (user-requested, same day).** Three
checks closed before any paid mapping run:

1. *Post-0006 exclusion, demonstrated live.* Two overlapping estates/trusts
   brackets (filing_status NULL, same chain) inserted in a rollback
   transaction: the second insert fails with sqlstate 23P01 on
   `no_overlapping_brackets` — the COALESCE(filing_status,'') arm is
   covering, so no migration 0007 is needed. The permanent pin already
   exists: `test_estate_trust_brackets_chain_without_filing_status`
   (tests/test_bracket_integrity.py:98) inserts the four-row estate chain
   and asserts the ExclusionViolation.
2. *Oracle-guard location.* The constructibility guard that maps all 128
   oracle entries onto CanonicalRecord lives at
   tests/accuracy/test_harness.py:157 — under tests/accuracy/ as anti-goal
   #1 requires; grep confirms no copy exists elsewhere.
3. *Neon parity restored.* Neon had 0001–0004; the runner applied
   0005_filing_status_qss and 0006_bracket_chain_taxpayer_class over the
   direct endpoint (schema_migrations now lists all six). The same
   overlap probe was then run against Neon in a rollback transaction:
   identical rejection (23P01, no_overlapping_brackets), zero rows left
   behind. Local and Neon enforce the same bracket integrity.

## 2026-08-25 — Phase 2 amendment: runtime multi-agent, bounded (mapper + verifier + adjudicator)

**Directed by the user before any code:** amend CLAUDE.md and write the ADRs
first. The semantic layer becomes a mapper + independent verifier pair; the
review queue gains an adjudicator; extraction and routing stay deterministic.

**Docs first.** A best-practices validation agent (Opus 5, web-sourced)
pinned every Anthropic citation against the live pages before the ADRs were
written. Three corrections it forced: "Building Effective Agents" now carries
a staleness banner (its principles are cited, its currency is not claimed);
the published multi-agent conditions are one three-item sentence plus a
separate breadth-first sentence, not a four-item list; and the
Opus-lead/Sonnet-workers mix is published *without rationale* — so the
orchestration-alignment ADR labels our all-Opus-workers reasoning as our own,
not Anthropic's. It also surfaced the Aug 2026 conformity-risk post ("when
one agent makes a bad decision, it is likely that many agents will make that
same bad decision"), which became the stated justification for the verifier's
different-model config rung. Committed: the CLAUDE.md amendment, ADR 012, and
adr-orchestration-alignment.md (every build-time and runtime orchestration
choice mapped to its published criterion; two deliberate deviations recorded).

**Build, key-less, same discipline as 2a.** Contracts first (ports, migration
0007's audit-trail columns and CHECK, triage's extra_findings entry, the
mapper's conventions/provenance law made shareable) so the fan-out had
disjoint files — the alignment ADR's deviation #2 applied to itself. Two Opus
5 builders in parallel: the verifier adapter (35 unit tests, fail-closed
verdict assembly, mapper confidence withheld to avoid anchoring) and the
adjudicator adapter (48 tests, the stamped-scan document as the unit
fixture; citation problems degrade, envelope problems raise). The
orchestrator did the pipeline wiring, per-role cost itemization, and the
accuracy harness's disagreement column.

**The stamped-scan test caught a real defect, twice independently.** The new
pipeline test (stamped-scan document through mapper -> verifier -> persist ->
adjudicate) failed: auto-resolutions vanished on connection close. Cause:
`list_open_reviews` ran a bare `execute` on the non-autocommit connection,
leaving it INTRANS; every later `transaction()` block silently degraded to a
savepoint and `close()` rolled the lot back — the pipeline would have
reported "auto_resolved" while the database kept the item open with no audit
trail. Minutes later the adjudicator builder reproduced the same bug
independently against the docker Postgres and reported it mid-flight. Fix:
the read commits its own transaction; the pipeline test and 13 DB tests pin
list -> resolve -> visible-from-a-fresh-connection, and migration 0007's
CHECK makes an unaudited resolved row unrepresentable.

**Adversarial review outcome (same day).** The same find-then-refute pattern
as 2a/2b: five Opus 5 finder lenses (correctness, anti-goals, API usage,
contract coherence, test honesty) produced 24 findings over the diff;
refute-by-default verifiers examined the 12 highest-severity and confirmed
3, each with a runnable reproduction or textual proof:

1. *Adjudication-pass containment (major).* Only `AdjudicationError` was
   caught, and only around the model call. Two unguarded routes — an SDK
   transport failure (`RateLimitError` is not a `RuntimeError`), and the
   repository's documented `ValueError` when an item stops being open
   between list and write — aborted the pass and discarded a PipelineResult
   whose records were already committed. Fix: per-item containment around
   both the call and the write; `disposition="error"` names the exception;
   four new tests on a port-faithful in-memory repository pin transport
   failure, the write race, and continuation.
2. *REJECT-class auto-close (major).* The pass applied one uniform rule to
   every open item, but a queue row born from a triage REJECT, an ingest
   refusal, or a mapping issue is the only live signal that a record is
   ABSENT from the fact table — and the adjudicator cannot restore records,
   so a truthful, well-cited, high-confidence adjudication would have closed
   the loss silently (anti-goal #8's exact shape, proven with an in-memory
   reproduction ending at 0 open rows and a missing record). Fix:
   auto-resolution restricted to items born from FLAG rules (record
   persisted as needs_review), default-deny on unknown reasons; everything
   else only ever receives a stored proposal. `FLAG_RULES` exported from
   validators and pinned; the adjudicator prompt now says when a proposal is
   advisory-only.
3. *ADR 012 cost overclaim (major).* The ADR stated a two-paid-calls-per-
   document ceiling; the code is one verifier call plus one adjudicator call
   per open queue item, unbounded in queue length. The ADR now states the
   per-item economics honestly (cached document context for items 2..n) and
   records the correction.

Nine findings were refuted with evidence (several were independent spellings
of the confirmed three, judged separately); twelve minors were reported
unverified and left unfixed, named in the workflow output — among them this
dev-log entry's own absence, which this entry closes.

**Design decisions worth recording:** verifier disputes are FLAGs, never
corrections (silence is never assent — an uncovered record comes back
DISPUTED "no verdict"); the mapper's confidence is withheld from the
verifier while the canonical conventions are shared verbatim (a dispute must
be about the document, never a prompt divergence about the target); the
adjudicator never decides — it returns a citated proposal and the pipeline
applies the threshold, with dangling citations never auto-resolving. The 2b
fan-out scope is updated: each per-fixture agent exercises the full
mapper+verifier path, the accuracy table carries the disagree column, and a
verifier outage fails the gate like a mapper outage.

**Gate state.** `make check`: 316 passed + the credential skip, ruff + mypy
strict clean. The accuracy run itself still waits on the funded API key
(`make accuracy` once it lands in .env) — unchanged from 2b, now through the
two-agent layer.

## 2026-08-25 — Gate condition: the twelve unverified minors, dispositioned

Rule applied (user-set): promote whatever touches persistence, cost
accounting, or auth; park the rest by name.

**Promoted and fixed (six minors, five fixes):**
- *Re-ingest re-pays for old open items* -> `list_open_reviews` is now a
  WORK list (`status='open' AND resolution IS NULL`): an item already
  carrying a stored proposal awaits its human and is never re-adjudicated;
  a second pass re-examines only never-proposed (e.g. previously errored)
  items. Pinned in memory- and DB-backed tests.
- *Failed adjudications invisible in pipeline_report* + *their spend
  unreported* -> `AdjudicationError` carries the cost the failed call still
  incurred (a truncated or malformed response was paid for; a transport
  failure that never got a response stays None), `AdjudicationOutcome`
  keeps it as `error_cost`, and pipeline_report gains an `adj_err` column,
  counts the spend into `adj_usd`, and names every failed item under
  "failed adjudications (items left open)" — non-fatal by design, since
  the item stays open and visible.
- *0007's CHECK guarded 'resolved' but not 'dismissed'* -> migration 0008
  replaces it with `closed_rows_carry_audit_trail`: ANY exit from 'open'
  needs who and when; 'resolved' additionally keeps its payload. Dismissal
  without an audit trail is now unrepresentable; tests cover both
  directions.
- *Mapper prices silently transferred to an overridden model* ->
  `SCHEMA_MAPPER_USD_*` now transfers only while the role runs the mapper's
  model; an overridden model without explicit role prices uses the
  defaults. Documented in .env.example, tested in both configs.
- *Verifier cache-read pricing branch untested* -> pinned (0.1x input
  price, exact arithmetic).
- *`list_open_reviews` "insertion order" overclaim* -> docstring corrected
  to what the code guarantees (created_at then id; ties inside one
  transaction timestamp fall back to id order).

**Already closed before this pass:** the dev-log absence (the amendment
entry above) and the unguarded repository ValueError (fixed with confirmed
finding #1).

**Parked as known minors (three), with reasons:**
- *Output ceilings ignore adaptive thinking*: ceilings are env-tunable;
  sizing them against thinking budgets is a measurement task that belongs
  with the Phase 3.5 latency work.
- *No pipeline-side check that verdict count equals record count*: the
  adapter constructs exactly one verdict per record fail-closed and the
  port validates 0..n-1 ordering; a pipeline recount would assert the same
  invariant twice.
- *Disagreement-column tests use a single group*: the column plumbing and
  totals are pinned; per-group attribution is exercised by the
  five-document accuracy run itself.

Post-fix: make check 328 passed + the credential skip, ruff + mypy strict
clean.

## 2026-08-26 — Phase 3: the API surface, hardened, contract-tested

**Scope shipped** (gate signed on the amendment; user directed proceeding
straight here): the full CLAUDE.md surface — POST /documents (202 + job id,
never blocking on extraction), GET /jobs/{id}, GET /records with filters and
cursor pagination, GET /documents (+/{id}), GET /records/resolve — plus the
hardening: X-API-Key on the POST (constant-time compare), CRON_SECRET bearer
on the sweep endpoint (the cron/queue-subscriber path), and the upload
guards (10 MB cap, %PDF magic, parse check, page cap) each firing before a
byte reaches the pipeline. Per-IP rate limiting stays a Vercel Firewall rule
for Phase 3.5, not application code.

**Design decisions:**
- *Read side is plain SQL, deliberately.* The pipeline's driven dependencies
  are ports because they swap per target; the API read model runs identical
  psycopg statements on RDS, Neon, and the local container — a read port
  would be an interface with one implementation. Recorded in
  api/queries.py's docstring.
- *Keyset pagination on (created_at, id)* — both immutable — so a walk is
  stable under concurrent inserts: nothing that existed at walk start is
  skipped or repeated, whatever lands mid-walk. Pinned by a contract test
  that inserts a second document between pages. Opaque base64 cursor;
  malformed cursors are 400, not 500.
- *Enqueue idempotency, three-valued:* live job -> returned as-is (the
  partial unique index settles the race, not a check); latest job succeeded
  -> no re-processing (re-upload is a no-op end to end); latest failed ->
  fresh job (re-upload is the retry path). All three pinned.
- *202-then-'missing credentials':* an upload against a misconfigured
  service is a valid upload — HTTP stays 202, and the truth lands on the
  job, typed (error.type == missing_credentials, message naming env VARS,
  never values). The contract test strips every credential var via
  monkeypatch so it passes identically on any shell; a live run through
  uvicorn reproduced it end to end.
- *Jobs and blobs:* BlobStore port (bytea adapter — the blob writes on the
  same connection as the document row), JobRunner port with NullJobRunner
  for request-scoped targets; sweep_pending claims with FOR UPDATE SKIP
  LOCKED so concurrent sweepers never double-process. The repository gained
  from_connection() (borrowed, close() a no-op) so the API's request
  connection serves the upload transaction.
- *OpenAPI 3.1* exported to docs/openapi.yaml (make openapi); a contract
  test regenerates the schema and diffs the committed file, so a stale
  export fails CI. Decimals serialize as exact-digit strings — for tax data
  exactness outranks a native JSON number, noted in the schema module.

**Two integration honesty notes.** FastAPI cannot resolve function-local
dependency aliases under `from __future__ import annotations` (the app
module documents why it omits the import). And starlette's TestClient is
typed against the installed httpx2, not httpx — the contract tests annotate
accordingly rather than casting.

**Gate evidence.** make check: 368 passed + the credential skip (40 API
contract tests), ruff + mypy strict clean. The two named gate assertions are
tests: tax_year=2026 excludes superseded (test_records.py) and pagination
stable across concurrent inserts (ibid.). Live demonstration against
uvicorn + the docker Postgres: a 3-page cursor walk over 7 seeded synthetic
records terminating in a null cursor; superseded hidden by default and
surfaced flagged with include_superseded; resolve returning [9001, 38000]
for amount 12500 (single) and the open-top estates/trusts bracket for
500000; POST unauthenticated -> 401; authenticated -> 202; sweep with the
bearer -> the job fails typed as missing_credentials. Router re-shown on
the five fixtures: 01-04 deterministic_text at $0/0 calls, 05 -> ocr
(tesseract, conf 0.92), report gate line green.

## 2026-08-26 — Accounting correction: the twelfth minor, named

The minors triage above reads "six promoted + three parked + two already
closed" — eleven. Twelve were reported. The twelfth is *"spend on failed
adjudications is never reported"*: it was promoted and fixed, but folded
into the same dev-log bullet (and the same fix) as its sibling *"failed
adjudications are invisible in pipeline_report"* — one bullet, two minors,
one mechanism (cost rides ``AdjudicationError`` into
``AdjudicationOutcome.error_cost`` and the report's ``adj_usd``). Correct
ledger: **seven promoted (six fixes), three parked, two previously closed
= twelve.** The disposition of every one is unchanged; only the header
under-counted.

## 2026-08-26 — Phase 4: the CDK stack, synthesized and validated offline

**Written conversationally** (the runbook's Phase 4 fan-out — the template
audit — deliberately deferred: it fires as its own ultracode run, on the
operator's word, over this committed tree). One environment-agnostic stack:
no account, no region, so a `fromLookup` is not merely forbidden
(anti-goal #4) but unrepresentable — grep shows zero call sites.

**Shape** (the IDP reference architecture, specialized): API Gateway (+ WAF
managed rules and a per-IP rate limit — the platform twin of the Vercel
Firewall hardening) -> ingest Lambda -> S3 documents bucket + Step
Functions Distributed Map (`max_concurrency=8`, the bottleneck knob) ->
extract (Textract) -> map+verify (Bedrock, the bounded ADR 012 pair) ->
persist (RDS PostgreSQL 18.3 via RDS Proxy, IAM auth, TLS) -> adjudicate.
Isolated VPC with interface/gateway endpoints and **no NAT**: no path to
the internet exists for a component handling tax documents, and the
endpoint list is exactly the service list. PG major pinned to 18 like every
other target; DB port 5433 like the local compose. Secret rotation is
implemented (hosted rotation, 30 days), not suppressed. Tracing is Lambda
active tracing + Powertools env — the X-Ray SDK appears nowhere
(anti-goal #6, grep-verified).

**Gate results, verbatim-verifiable:**
- `cdk synth` exit 0 with AWS credentials stripped from the environment
  (`env -u AWS_ACCESS_KEY_ID ...`); cdk-nag AwsSolutionsChecks runs as an
  aspect, so synth green *is* nag clean.
- NagReport: 48 compliant, 47 suppressed, **0 non-compliant**. Seven rule
  ids suppressed, each with its written justification in the stack source
  (and recorded in the committed CSV): IAM4 (scoped to the three AWS
  service-role managed policies, log/ENI baseline), IAM5 (three wildcard
  classes: Textract has no resource-level permissions; Bedrock scoped to
  foundation-model/anthropic.* with the cross-region-profile region
  wildcard; CDK grant shapes), APIG2/APIG4/COG4 (proxy integration with
  app-layer Pydantic validation; public read-only GETs by design with
  X-API-Key + WAF on the write path; no user pool exists), L1 (runtime
  pinned to the project's tested 3.12), and the EC23 validation-failure
  warning on endpoint SGs (allow-from-VPC-CIDR token in a no-IGW VPC). A
  drafted S1 suppression turned out dead (cdk-nag scores the log-target
  bucket compliant) and was removed — no unused suppressions.
- `cfn-lint` exit 0 on the synthesized template. W3045 was FIXED, not
  ignored (serverAccessLogsUseBucketPolicy flag + BUCKET_OWNER_ENFORCED);
  W3005 — CDK's own redundant DependsOn emission — is ignored with a
  written justification in infra/.cfnlintrc.
- CI gains an `infra` job running `make synth-check` on every push;
  GitHub runners hold no AWS credentials, so the offline property is
  enforced by the environment, not assumed.
- `infra/cdk.out/` committed (template, manifest, NagReport CSV, staged
  assets).

**Discoveries en route:** cdk-nag 3.0 removed the `NagSuppressions` helper
that carries per-resource written justifications — pinned `<3` with the
reason in pyproject; granular validation-failure suppression takes the rule
in `appliesTo`, not the id; the S3 log-delivery feature flag is what
retires the legacy ACL property.

**Open item, stated plainly:** the Lambda handler modules
(`tax_tables.aws.handlers.*`) and the Textract/Bedrock adapters are the
deploy-time artifact contract, tracked with Phase 5's hexagonal proof; the
stack docstring and the README will keep saying the design synthesizes and
validates but was never deployed. ADR 007 (CDK over Terraform) written.

## 2026-08-26 — Phase 4 ultracode audit: 23 agents, 14 confirmed, 0 refuted

The operator-triggered audit workflow ran over the committed tree: six
resource-type auditors + three directed lenses (suppression justifications,
currency of hardcoded facts, isolated-VPC completeness), then
refute-by-default verification. 75 raw findings; the 14 highest-severity all
CONFIRMED — an 0-for-14 refutation rate that says the finder pool was
precise, and that synth+cfn-lint+nag green is nowhere near deploy-correct.
The 14 collapse to eight distinct defects, every one foreclosed test-first
(tests/infra/test_stack.py, aws_cdk.assertions over the same feature-flag
context cdk.json synthesizes under; all eight failed red before the fixes):

1. *Flow-log delivery grant missing (critical).* The verifier decompiled
   aws-cdk-lib 2.266's vpc-flow-logs.js to prove the mechanism: the
   delivery.logs.amazonaws.com statements are gated on the
   createDefaultLoggingPolicy feature flag, not on who created the bucket —
   and the flag was unset. Per AWS docs (quoted verbatim in the finding),
   deploy either fails CreateFlowLogs or the service OVERWRITES the bucket
   policy, silently destroying the stack's own enforce-SSL Deny and the
   access-log grant. Fix: the flag, now set; the template carries both
   delivery statements.
2. *RDS Proxy port mismatch (critical; four agents independently).* RDS
   Proxy for PostgreSQL listens on 5432 regardless of the target's port;
   wiring lambdas to 5433 meant nothing could ever connect. Fix: PROXY_PORT
   constant, lambda->proxy on 5432, proxy->instance stays 5433, DB_PORT env
   corrected.
3. *Rotation could never reach the database (five agents).* The hosted
   rotation Lambda sat in the shared Lambda SG with no path to the DB — the
   30-day schedule would fail forever, silently. Fix: dedicated RotationSg,
   the only non-proxy principal the DB admits.
4. *Master-user authentication (critical).* Every Lambda held
   rds-db:connect on the MASTER user (an rds_superuser member on RDS
   Postgres). Fix: grants and env moved to the least-privilege app_ingest
   role; the role's creation is a deploy-time migration, recorded in the
   README limitations.
5. *WAF blocked every real upload (critical).* SizeRestrictions_BODY
   blocks bodies over 8 KB in Block mode — in front of a 10 MB PDF intake.
   Fix: rule override to Count, justification in source (the app's own
   guards enforce the cap before a byte reaches the pipeline).
6. *PDF bodies UTF-8-mangled (critical).* No BinaryMediaTypes on the REST
   API. Fix: application/pdf declared.
7. *One bad document aborted the whole batch (critical).* No
   tolerated-failure on the Distributed Map. Fix:
   tolerated_failure_percentage=100 — the same per-document isolation the
   local pipeline enforces.
8. *The deploy-artifact contradiction (critical).* The source claimed a
   dependency layer that exists nowhere. Fix: the comment now states the
   truth (the layer is a deploy-pipeline step that intentionally does not
   exist), and the limitation ships in the README; the handler half closes
   with the deploy-time contract work below.

The completeness lens produced a 33-row endpoint inventory (every runtime
AWS call vs the seven endpoints, local-signing paths called out); its two
deliberate gaps (bedrock:GetInferenceProfile, cloudwatch:PutMetricData) are
documented in the README's honest-limitations section rather than papered
over with endpoints nothing uses. 61 lower-severity findings reported
unverified in the workflow output; the two unverified criticals duplicate
defect 8. CI's check job now installs the infra group so the audit pins run
(a skip in CI would be a silent lie); post-fix: synth exit 0 credential-
stripped, cfn-lint clean, nag 51 compliant / 47 suppressed / 0
non-compliant, make check 377 passed + the credential skip.

## 2026-08-26 — The deploy-time contract closed, keyless

The audit's remaining critical (handlers absent from the asset) and
CLAUDE.md's "AWS adapters real, complete, unit-tested" both close in one
motion, all of it credential-free:

**Textract TableExtractor** (40 tests): parses the documented
BLOCK/CELL/RELATIONSHIPS shape — merged-cell continuations to None (the
shared convention), cell confidence as the MIN of word confidences (mean
rewards dropout, the 2a lesson), prose grouped and classified by the shared
heuristic, OcrPageStats from word confidences, one AnalyzeDocument call per
page priced like every other engine. Tested against
fixtures/textract/05_response.json — HAND-CONSTRUCTED per CLAUDE.md's
standing instruction and labeled as such in the JSON itself, its generator,
and the tests; content transcribed from the real scanned fixture via the
local OCR (285 words, matching the local pass exactly; the oracle was never
opened). The generator regenerates the file byte-identically. Honest
deviations recorded in the generator docstring: the merged cell models
Textract's caption-band folding (document 05's real header is flat), and
one glyph the local engine misread is transcribed correctly rather than
replicating another engine's error into this engine's modelled response.

**Bedrock semantic adapters** (28 tests): the hexagonal proof at its
thinnest — anthropic's AnthropicBedrock client injected into the SAME three
adapters; zero duplicated prompts, schemas, parsers, or cost math. Per-role
timeouts imported (not copied) from the adapters so drift fails loudly; the
"sigv4" sentinel satisfies the config's key requirement while documenting
that auth is SigV4; model-id drift between the stack and the factories is
pinned mechanically (a test AST-parses the stack). Structured-outputs
acceptance by a live Bedrock runtime is named as a deploy-time
verification item, with fail-closed parsers guaranteeing a loud failure.

**tax_tables.aws.handlers** (8 tests): the five entry points the template
addresses, recomposing the shared pipeline at the state boundaries from
its now-public pieces (dispute_findings, issue_entry,
adjudicate_open_items). A test pins every template handler string to a
callable. StepFunctionsJobRunner stages the blob to S3 and starts the
execution, and never raises past a 202 the client already earned (the
queued row is the recovery path — tested). The extract handler pins the
router's economics on AWS: a text-layer document makes ZERO Textract calls
(the fake explodes if touched). persist_records preserves the accounting
invariant against the real database; adjudicate_queue keeps the shared
FLAG-only auto-resolve rule. CanonicalRecord's strict mode forced the
payload round-trip through JSON validation — recorded because it is the
kind of seam a split pipeline hides until it doesn't.

Wiring: `aws` extra (boto3, mangum — never in the Vercel bundle), the
cloud assembly re-synthesized (the src/ asset now ships the handlers),
README.md created with the honest-limitations section: never deployed, the
dependency-layer/secrets/role-creation deploy gaps, the endpoint inventory
with its two deliberate omissions, and the credential-blocked accuracy
gate. Post-everything: make check 453 passed + the credential skip; synth
exit 0 credential-stripped; cfn-lint clean.

## 2026-08-26 — Disposition of the 61 unverified audit findings

The Phase 4 audit named 75 findings and verified 14. The remaining 61 were
reported unverified. Rather than fan out again, they got a title-level
sweep against three criteria — **data loss, money paths, auth** — and
everything that plainly touched one was then verified *against primary
sources*, not accepted on the finder's word. The full ledger, with every
finding's disposition, is committed at
[`docs/audit/2026-08-26-phase4-findings.md`](audit/2026-08-26-phase4-findings.md):
27 confirmed-and-fixed, 21 promoted-and-fixed, 1 promoted-and-documented,
3 refuted, **23 parked as named-but-unverified by design**. A parked row is
not a claim about the stack; nothing in the README or this log rests on one.

Promoted and fixed (each verified first):

- **Lambda throttling is unretried.** Decompiled aws-cdk-lib 2.266's
  `invoke.js`: `retryOnServiceExceptions` adds exactly
  ClientExecutionTimeout / Service / AWSLambda / SdkClient.
  `Lambda.TooManyRequestsException` is absent — and a throttle is the
  *expected* failure at MaxConcurrency 8. Retry added.
- **No job ever recorded a failure** (see the previous entry).
- **The fan-out could starve the read path.** MaxConcurrency bounds one Map
  Run, not the account; with nothing reserved, a batch and the public GETs
  drew the same unreserved pool. Reserved concurrency on all six functions.
- **The stated connection-exhaustion mitigation was unset.**
  `ConnectionPoolConfigurationInfo` synthesized empty, so "RDS Proxy pools
  connections" was a claim, not a setting. Pool sized to the fan-out.
- **`allow_all_outbound=False` did not survive synth.**
  `SecurityGroup.addEgressRule` calls `removeNoTrafficRule()` *before* the
  branch that emits an SG-peer rule as a separate `CfnSecurityGroupEgress`
  — so the inline `255.255.255.255/32` placeholder was deleted with nothing
  put in its place, and AWS is explicit: "The default rule is removed only
  when you specify one or more egress rules." The proxy had allow-all
  egress while the source said otherwise. The placeholder is re-asserted
  after the last construct that touches the group, and pinned by a test,
  because anything added below would strip it again just as silently.
- **Every VPC endpoint used the default full-access policy.** The stack's
  docstring claims no path to the internet exists for a component handling
  tax documents; an unrestricted S3 gateway endpoint is exactly such a path
  — it will carry a PUT to any bucket in any account. All seven endpoints
  now require `aws:PrincipalAccount` to be this account. Deliberately not
  action-scoped: an over-tight endpoint policy is a deploy-time failure
  this project cannot test, and that trade-off is in the README.
- **The IAM4 suppression's justification was false.** Quoted from the
  published policy documents: `AmazonAPIGatewayPushToCloudWatchLogs` grants
  `logs:GetLogEvents` and `logs:FilterLogEvents` on `Resource "*"` —
  account-wide log *read*, not the "log-write only" the reason claimed; and
  both Lambda policies grant on `"*"`, not the function's own log group, so
  "no privilege reduction" was wrong for the logs half (every function here
  has an explicit LogGroup). The API Gateway role now has its own accurate
  suppression; the Lambda reason states what each policy actually grants,
  which half can be narrowed, and why it is accepted anyway.
- **The IAM5 suppression was blanket.** No `appliesTo` meant one entry
  pre-excused every wildcard grant nobody had written yet. Now enumerated
  (15 findings). It proved itself immediately: enabling the Map Run export
  produced a new S3 wildcard and *failed the synth* instead of passing
  silently. That is the feature — and the reason the export got its own
  bucket, since `ResultWriterV2` grants `PutObject` on the whole
  destination and the audit-log bucket must not be writable by the pipeline.
- **The Map Run export.** `result_writer_v2` needs the
  `@aws-cdk/aws-stepfunctions:useDistributedMapResultWriterV2` context flag;
  without it CDK accepts the argument and emits no `ResultWriter` at all.
  Verified: the ASL had none until the flag went into `cdk.json`.
- **The tracing claim contradicted anti-goal #6.** The docstring said
  tracing was "Powertools Tracer ... the SDK never appears in the bundle" —
  self-defeating, because Powertools' Tracer wraps `aws-xray-sdk`. Tracing
  here is Lambda *active* tracing, a platform setting with no library; the
  `POWERTOOLS_*` variables configure Logger. Corrected in place. This one
  is outside the three criteria and was promoted anyway: it is a written
  claim that contradicts a hard constraint.

Refuted on verification: the Textract single-page sync limit (the adapter
renders each page to PNG, so no PDF ever reaches that path); the cdk-nag
`<3` pin being unrecorded (it is, in both `pyproject.toml` and ADR 007);
and the EC23 suppression over-reach (its effective scope today is exactly
the six endpoint security groups its reason names).

Post-sweep: `make check` 469 passed + the credential skip; synth exit 0
credential-stripped; cfn-lint clean; cdk-nag 58 compliant / 53 suppressed /
0 non-compliant.

## 2026-08-26 — Phase 5a: diagrams, README, ADR consolidation

Everything in Phase 5 that does not depend on a live run. The two open gates
(2b accuracy, 3.5 deploy) are named as open in every place a number would
otherwise go, and marked `TBD` with the command that fills them.

**The C4 diagrams do not use Mermaid's C4 syntax, and that was a finding, not
a preference.** GitHub renders Mermaid client-side with its own bundle, and
that bundle does not include the C4 plugin — `C4Context` / `C4Component` blocks
render as raw text on GitHub while rendering *perfectly* in the Mermaid live
editor and in `mermaid-cli` ([community discussion
#197898](https://github.com/orgs/community/discussions/197898), closed
unanswered, 2026-06-03; verified by rendering a C4 block locally, which
succeeded and proves nothing). That asymmetry is the trap CLAUDE.md
anticipated. So Level 1 and Level 3 are plain `flowchart`s applying C4
semantics through subgraph boundaries and typed `[Person]` / `[Component]` /
`[Port]` labels.

"Verify they render" became a repeatable check rather than a claim:
`scripts/check_diagrams.py` (`make diagrams`) extracts every ```mermaid block
from the README and parses it under **two Mermaid majors, 10 and 11**, which
brackets whichever version GitHub ships; it also fails outright on a C4 block,
so nobody re-introduces one. Result: 2 diagrams, 0 failures, both versions.

Two things the Level 1 diagram shows that are uncomfortable and are drawn
anyway: the AWS system is dashed because it was designed and validated but
never deployed, and the reviewer reaches the review queue **through the
database**, because the queue has no HTTP endpoint. Drawing the second one
honestly is what surfaced it as an honest-limitations item.

**README.** Written around the four evaluation criteria with a table pointing
at where each is answered. The bottleneck section is the one that took the
work: it gives the arithmetic (10,000/day, 60% inside a four-hour window,
0.42 documents/second, required concurrency = 0.42 x T) and then names what
breaks in order — model-provider TPM first (~1.26M tokens/minute at that rate,
above default org tiers), then fan-out concurrency and the reserved-concurrency
fix, then connection exhaustion and why RDS Proxy and the pooled Neon endpoint
are in the design at all, then the Step Functions 256 KB payload quota, then
ingest volume at 100 GB/day. `T` is a `TBD` fed by the accuracy run, so the
table is algebra with a named unknown rather than invented numbers.

Also consolidated into the README: the fixture-design disclosure (the five PDFs
and the ground truth are **self-authored**, with the traps documented as test
engineering), the three-targets section naming both open gates, and a
thirteen-item honest-limitations section that now includes the endpoint
inventory, the AWS-target upload ceiling (~4.4 MB, not 10 MB — API Gateway
base64-encodes the body into Lambda's 6 MB synchronous payload), the accepted
IAM over-grant stated in full, and the missing review-queue endpoint.

**ADRs.** Ten written, taking the set to twelve. The three rejections each
record the threshold at which they would flip, and each rests on a primary
source rather than recollection:

- **Aurora DSQL** — its published `CREATE TABLE` grammar's `table_constraint`
  production is `CHECK | UNIQUE | PRIMARY KEY`. No `EXCLUDE`, no `REFERENCES`.
  And its supported-types page enumerates the subset it supports, in which
  **range types do not appear at all** — so `bracket int8range` is
  unrepresentable and the centrepiece constraint cannot be written.
- **RDS Data API** — Aurora-only, so a *driver* choice would silently make a
  *compute* choice; and it needs a second `RecordRepository` implementation
  (not a second adapter over one driver) whose type coercion could never be
  integration-tested here. An untestable divergent path is where silent
  corruption lives.
- **Aurora Serverless v2** — the decisive fact is not the latency, it is that
  AWS documents auto-pause and RDS Proxy as mutually exclusive: "If your
  Aurora cluster has an associated RDS Proxy... any Aurora serverless
  instances in such a cluster won't automatically pause." The headline benefit
  is unavailable in exactly the architecture that would use it. The latency
  numbers ("approximately 15 seconds", "30 seconds or longer" after 24 h) and
  the ~$43.80/month 0.5-ACU floor are the supporting arguments, not the
  primary one.

Post-5a: `make check` 469 passed + the credential skip; `make diagrams` 2/2
under both majors; all internal doc links resolve.
