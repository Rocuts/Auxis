# Tax Table Ingestion Service

## Mission

Accept PDF documents containing tax tables, extract the tabular data, normalize it
into a canonical schema, persist it, and expose it over a REST API.

This is a take-home exercise for a Senior AI Engineer role. Treat it as a
**product**, not a demo. Evaluation criteria:

1. Clear backend and API design
2. Practical approach to PDF extraction and normalization
3. Reasoning about parallel processing and bottleneck mitigation
4. Documentation of development steps and tool choices

Deliverables: a live service URL, a README, and a C4 Components diagram in Mermaid.

---

## Hard constraints — settled, do not re-litigate

If you believe one of these is wrong, say so in one sentence and proceed anyway
unless told otherwise.

| Decision | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Best PDF ecosystem; one language across app + IaC |
| Packaging | `uv` | Fast, lockfile, reproducible — and Vercel's Python builds consume `pyproject.toml`/uv natively |
| API framework | FastAPI + Pydantic v2 | OpenAPI 3.1 for free; first-class framework on Vercel |
| Database | PostgreSQL 16/17 | Range types + GiST exclusion constraints; pin the local image to the same major as the Neon branch |
| DB driver | `psycopg` 3 | Identical code on AWS, Vercel, and local |
| Migrations | Plain versioned `.sql` + tiny runner | The DDL is itself a deliverable; keep it readable |
| IaC | **AWS CDK v2 in Python** | `cdk synth` runs with no credentials; Terraform's `plan` requires provider auth, and `validate` alone proves far less than a full synth |
| AWS target | Lambda + Step Functions + S3 + RDS PostgreSQL + RDS Proxy + API Gateway | Matches AWS's own IDP reference architecture |
| Lambda toolkit | Powertools for AWS Lambda (Python) — **Logger only**, base extras | Structured logging with per-document correlation keys, in the AWS handlers. Narrowed from the original row after a claim-vs-lockfile check (ADR 013 addendum): **idempotency** already lives at the data layer (SHA-256 natural key, `jobs_one_live_per_document`, `UNIQUE NULLS NOT DISTINCT`) and **batch partial failure** at the Distributed Map (`tolerated_failure_percentage` + per-step Catch), so neither of those utilities has anything here to attach to. Never the `tracer` or `all` extras — they pull the SDK anti-goal #6 forbids. |
| Live demo host | **Vercel — Python/FastAPI on Fluid compute** | Named in the brief itself; already licensed (Pro); Git integration gives a preview deploy per PR |
| Demo database | Neon Postgres via the Vercel Marketplace integration | Standard Postgres with `btree_gist`; free tier; connection env vars injected by Vercel |
| Async jobs on the demo | Vercel Queues (Python SDK); cron-sweep fallback | Closest platform analog to the Step Functions fan-out; keeps `POST → 202` honest on request-scoped compute |

**Evaluated and rejected** (each needs an ADR):

- **Aurora DSQL** — no exclusion constraints, extensions, views or foreign keys
  (cite the official unsupported-features page). The bracket integrity model
  depends on exclusion constraints.
- **RDS Data API** — Aurora-only; would require a second persistence adapter and
  break single-codebase portability.
- **Aurora Serverless v2** — scale-to-zero has ~15s resume latency, which would
  make the live demo URL appear broken on first request; keeping min ACU above
  zero costs ~$43.80/month for a 128-record database. Document the threshold at
  which migrating becomes correct.
- **Terraform / CDKTF** — `plan` cannot run without AWS credentials; `validate`
  runs offline but checks syntax, not a synthesized template.
- **Long-running container host for the demo** — a resident worker process would
  fit the in-process JobRunner, but free container tiers cold-start after idle,
  reproducing exactly the failure mode the Serverless v2 ADR rejects. Vercel +
  Queues solves async without a resident process.

---

## Anti-goals — these fail the review

1. **Never read `fixtures/ground_truth.json` from anywhere except
   `tests/accuracy/`.** It is the test oracle. If any module under `src/` imports,
   opens, or embeds values from it, the work is invalid. Every extracted value must
   be derived from the PDF itself. Assume this will be verified with `grep`.
2. **Never weaken a test to make a gate pass.** If accuracy is 126/128, report the
   two failures and why. A truthful red result is worth more than a green one that
   was negotiated.
3. **No Terraform / CDKTF.** AWS CDK only.
4. **No `*.fromLookup()`, `Vpc.fromLookup`, or AMI lookups.** These trigger AWS API
   calls during synth and break the offline-synth requirement.
5. **Never run `cdk deploy`, `cdk bootstrap`, or any command that mutates AWS.**
   There is no account and no budget.
6. **Do not use the AWS X-Ray SDK — directly or transitively.** It entered
   maintenance mode in February 2026, and this binds the *dependency graph*, not
   just imports: `aws-lambda-powertools[tracer]` and `[all]` pull
   `aws-xray-sdk`, so Powertools may be depended on only WITHOUT those extras
   (its base distribution requires just `jmespath` and `typing-extensions`).
   Tracing on the AWS target is Lambda **active tracing** — a platform setting
   that ships no library. If in-code spans are ever needed, use OpenTelemetry;
   never Powertools Tracer, which is a wrapper over the forbidden SDK.
   `tests/test_tracing_policy.py` enforces this against `pyproject.toml`,
   `uv.lock`, and `src/` (ADR 013).
7. **Do not build a generic "any document" system.** Scope to the five document
   shapes below. Generality is a cost here, not a virtue.
8. **Never silently drop or guess a value.** A cell that cannot be parsed
   confidently goes to a review queue with its provenance attached. Quiet data loss
   is the worst possible failure mode for tax data.
9. **Never run `vercel deploy --prod`, `vercel promote`, `vercel env` mutations, or
   any other production-mutating command from inside a workflow or goal loop.**
   Preview deploys through prompt-approved commands are acceptable; promotion to
   production is always a human action.
10. **Never commit secrets.** `.env` never enters git; `.env.example` documents
    every required variable (`DATABASE_URL`, `ANTHROPIC_API_KEY`, `API_KEY`,
    `CRON_SECRET`). The Anthropic key is server-side only and must never reach a
    response body, a log line, or the client.

---

## The input documents

Five PDFs in `fixtures/`. They are **deliberately heterogeneous** — each breaks a
different naive assumption. Read them; do not assume.

| File | Shape | What it breaks |
|---|---|---|
| `01_..._TY2026.pdf` | Wide matrix, 7 rate rows x 4 filing-status columns, two-level header with merged cell | One visual row is four logical records. Top bracket open-ended (`and over`) |
| `02_..._TY2026.pdf` | Small tables + prose | Amounts have no `$`. One rule exists only in a prose sentence. Carries a prior-year column |
| `03_..._2026.pdf` | 51 rows across 2 pages | Repeated header + `(continued)`. Rates have no `%` — unit stated only in body text. Long dash means *no tax imposed* (NULL), not zero. One legitimately negative rate. One derived column |
| `04_..._2026.pdf` | Landscape, four separate tables on one page | Mixed units in one document. `No limit` as a value. Records that are not brackets at all |
| `05_..._TY2025.pdf` | **Scanned image, no text layer** | `pdfplumber` returns nothing. Different range separator (`to`). One rate exists only in a footnote. Document is **superseded** and must not surface in `tax_year=2026` queries |

Two traps stated explicitly because they are easy to miss:

- Document 02 carries the ID `TB-2025-14` but applies to **tax year 2026**. Tax
  bulletins are issued in November of the preceding year. `tax_year` must be read
  from the effective-date sentence in the body — **never** inferred from filename
  or document ID.
- Documents 02 and 04 each hold two tax years in one row (current + prior column).
  An extractor that keeps one column silently loses half the records.

`fixtures/ground_truth.json` documents the target schema, field conventions, and a
`deliberate_traps` array. Read it to understand the target and to build the
harness. See anti-goal #1.

---

## Architecture

### Ports and adapters, strictly

The domain and pipeline must not know AWS or Vercel exists. Each port has three
implementations, selected by configuration:

| Port | AWS adapter (designed, synth-only) | Vercel adapter (live URL) | Local adapter (docker-compose) |
|---|---|---|---|
| `BlobStore` | S3 | Postgres `bytea` (default; see ADR) or Vercel Blob | local filesystem |
| `TableExtractor` | Textract (`AnalyzeDocument`, TABLES) | `pdfplumber` (digital) / **vision-OCR, Anthropic Messages protocol** (scanned) | `pdfplumber` (digital) / Tesseract (scanned) |
| `SchemaMapper` | Bedrock | AI Gateway · `zai/glm-5.3-flash` | AI Gateway · `zai/glm-5.3-flash` |
| `RecordVerifier` | Bedrock | AI Gateway · `alibaba/qwen-3-235b` | AI Gateway · `alibaba/qwen-3-235b` |
| `Adjudicator` | Bedrock | AI Gateway · inherits the mapper | AI Gateway · inherits the mapper |
| `JobRunner` | Step Functions Distributed Map | **Vercel Queues** (fallback: cron sweep) | in-process worker pool |
| `RecordRepository` | psycopg → RDS Proxy | psycopg → Neon (pooled endpoint) | psycopg → Postgres container |

The three semantic roles speak the **Anthropic Messages protocol**; which
endpoint answers it is configuration. Live and local, that is the **Vercel AI
Gateway** — `zai/glm-5.3-flash` for the mapper and adjudicator,
`alibaba/qwen-3-235b` for the verifier (ADR 014). **Direct Anthropic**
(`api.anthropic.com`) and **Bedrock** (AWS, designed-only) are the other two
config-selected routes: wired, and neither funded here. No direct-Anthropic
key is provisioned on this project — a claim of one would be a claim of a
route that does not exist.

The Vercel adapters run the live demo. The local adapters run the test suite and
the evaluator's `docker compose up`. The AWS adapters must be real, complete,
and unit-tested against recorded fixtures — not stubs. Record one real Textract
response for document 05 into `fixtures/textract/05_response.json` if credentials
ever become available; until then, hand-construct a representative fixture from
the documented `BLOCK` / `CELL` / `RELATIONSHIPS` shape and label it as such.

### The three deployment targets

One domain, three targets — this is the hexagonal claim *proven*, not asserted,
and it is a README headline:

1. **AWS** — designed in full, expressed as a CDK stack that synthesizes and
   validates offline. Never deployed; the README says so plainly.
2. **Vercel** — the live URL. Fluid compute, Queues, Neon.
3. **docker-compose** — the evaluator's one-command reproduction: API + Postgres +
   worker + the five fixtures ingested. This must work from a fresh clone.

### Vercel constraints that shape the adapters

- **Functions are request-scoped.** There is no resident worker process, so the
  in-process `JobRunner` cannot serve the live URL. `POST /documents` persists the
  document and job rows, enqueues one Vercel Queues message per document, and
  returns **202** immediately; a subscriber endpoint runs the pipeline per message
  and updates job status. Concurrent messages in flight are the demo's parallel
  processing — the direct analog of the Distributed Map fan-out, and it belongs in
  the bottleneck section. If Queues turns out to be unavailable on the team, fall
  back to a jobs-table sweep driven by a `vercel.json` cron (minute granularity)
  and record the latency trade-off in the ADR.
- **No system binaries.** Tesseract cannot be installed into a Vercel function, so
  document 05 uses a **vision-OCR `TableExtractor` adapter** speaking the
  Anthropic Messages protocol (endpoint by config, as for the semantic roles).
  This does not bend the no-pixels rule — restate it precisely: *the rule binds
  the `SchemaMapper`*, which only ever sees an extracted cell grid.
  `TableExtractor` adapters are the components licensed to read pixels — Textract
  is itself ML-based OCR, and the vision adapter is its platform equivalent.
  Every mapped value still traces to a grid cell an extractor produced, and
  document 05 remains the only document with nonzero extraction cost on any
  target. The router must never send a document with a usable text layer to the
  vision adapter.
- **Bundle hygiene.** Use `excludeFiles` in `vercel.json` to keep `fixtures/`,
  `tests/`, and docs out of the function bundle. The ground truth never ships in
  the deployed artifact — this reinforces anti-goal #1 at the packaging layer.
- **Connections.** Use Neon's pooled connection string from functions; keep
  connections short-lived per invocation. Verify psycopg prepared-statement
  behavior against the pooler at the Phase 1 gate (`prepare_threshold` if needed).
- **Duration.** Set `maxDuration` in `vercel.json` for both the app entrypoint and
  the queue subscriber, sized to the slowest single-document pipeline run plus
  margin. Measure, don't guess.

### The extraction router — the core insight

**Four of five documents have a text layer.** Running a paid table-extraction API
on them is waste: `pdfplumber` reads the actual PDF table structure with higher
fidelity than any model inferring it from pixels, at zero cost.

```
has usable text layer AND tables detectable
  -> deterministic extraction (pdfplumber). No AI service. $0.
otherwise
  -> OCR path (Textract on AWS / vision-OCR on Vercel / Tesseract local)
```

Then, for **both** paths:

```
extracted cell grid -> mapper (LLM) -> independent verifier (LLM) -> validators
  -> persist / review queue -> adjudicator (LLM, single pass over the queue)
```

**The `SchemaMapper` never reads a number off an image and never invents a
value.** It receives an already-extracted cell grid and decides *what each cell
means* — which column is a rate, which is a bound, which filing status a column
belongs to, whether a dash means null. Semantic mapping only. Every numeric value
must trace to a cell the extraction layer produced.

Track per-document cost itemized by role — extraction, mapper, verifier,
adjudicator — separately. "4 of 5 documents cost $0 to extract on every target"
is a headline finding for the README, not a footnote.

### The semantic layer — bounded runtime multi-agent (amended 2026-08-25)

The semantic layer is a **mapper + independent verifier pair**, and the review
queue gains an **adjudicator**. Exactly three model roles, each justified
against Anthropic's published criteria for multi-agent systems —
parallelizable work, specialization, context isolation (ADR 012) — and nothing
else in the pipeline is an agent:

- **`SchemaMapper`** (as above): semantic mapping only. Unchanged contract.
- **`RecordVerifier`**: receives each document's mapped records plus the *same*
  grid/prose context the mapper saw — in its own context window, under a
  skeptic prompt, with no access to the mapper's reasoning. It confirms or
  disputes each record's values and provenance citations. *Context isolation*
  is the point: an independent re-derivation that agrees is evidence; a model
  reviewing its own transcript is an echo. A dispute is a FLAG, never a
  correction — the record persists as `needs_review` and the dispute reaches
  the review queue with its reason (anti-goal #8: the verifier also never
  drops or rewrites a value). Config permits a cheaper or different-family
  model than the mapper (`RECORD_VERIFIER_*` env).
- **`Adjudicator`**: a single pass over open review-queue items. It re-examines
  one item at a time with the full extracted evidence and proposes a citated
  resolution; at or above a confidence threshold the item auto-resolves with a
  full audit trail (resolution, citations, `resolved_by`, `resolved_at`);
  below it the item stays with a human, the proposal stored for the reviewer.
  Auto-resolution applies only to items whose record actually persisted
  (FLAG findings): a queue row standing for data the fact table refused or
  never received (rejects, conflicts, mapping issues) is the only live signal
  of the loss and never auto-closes.
  *Specialization*: queue triage over one item with full evidence is a
  different task than batch mapping, with a different prompt objective.
  Citations are validated against the extracted document; a resolution with a
  dangling citation never auto-resolves.

**Why extraction and routing stay deterministic:** the simplest thing that
works is not an agent (Anthropic's first published criterion). The router is a
two-branch rule over measurable page properties; `pdfplumber` reads actual PDF
structure with higher fidelity than any model inferring it from pixels, at $0
and reproducibly. An agent there would add nondeterminism, latency, and
per-document token cost with no accuracy left to buy. Model spend is reserved
for the one layer where semantic judgment lives.

**Bounds, explicitly:** three roles, each single-pass — no loops, no
agent-to-agent negotiation, no self-correction cycles. A mapper/verifier
disagreement is never settled by the models talking; it routes to the review
queue. The adjudicator never edits records — it resolves queue items,
auditably, or leaves them for a human.

### Data model

One canonical fact table: typed core plus a JSONB tail for type-specific
attributes. Not eleven tables (rigid — every new document shape needs a migration),
not one blob (no constraints, no type safety).

Typed core carries: provenance (document, page, table), temporal validity
(`tax_year`, `effective_from`, `effective_to`, `lifecycle_status`), `jurisdiction`,
`record_type` discriminator, `attribute_key` sub-discriminator, value slots
(`bracket int8range`, `rate numeric`, `amount numeric`), plus `confidence` and
`review_status`.

**The centerpiece:** an `EXCLUDE USING gist` constraint making overlapping brackets
*impossible at the database level* for the same (jurisdiction, record_type,
tax_year, filing_status, taxpayer_class) chain. Bracket overlap is not validated in
application code — it is unrepresentable. Requires `btree_gist`, which Neon
supports; `CREATE EXTENSION btree_gist` on the Neon branch is part of the Phase 1
gate.

Gap-freeness cannot be an exclusion constraint (cross-row aggregate) — put it in a
validation step and a diagnostic view.

Note the rate domain must permit small negatives: document 03 contains a
legitimate negative local rate reflecting a statutory rebate. A naive `rate >= 0`
check rejects valid data.

Idempotency at two levels: SHA-256 of the document as its natural key (re-uploading
the same PDF is a no-op), and a `UNIQUE NULLS NOT DISTINCT` natural key on records.

### API surface

- `POST /documents` → **202 + job_id**. Never block the request on extraction.
- `GET /jobs/{id}` → status, counts, errors
- `GET /records` → filters (`tax_year`, `jurisdiction`, `record_type`,
  `filing_status`, `effective_on`, `include_superseded`, `min_confidence`),
  **cursor pagination**, not offset
- `GET /documents`, `GET /documents/{id}` → provenance
- `GET /records/resolve?amount=&filing_status=&tax_year=` → returns the bracket
  containing the amount. Proves the range index and bracket integrity deliver
  something real. Return the applicable bracket record — this is a data lookup, not
  tax advice, and the response must not read as a computed tax liability.

`GET /records?tax_year=2026` must return active 2026 records and exclude superseded
ones. That is a test, not a claim.

**Hardening — the URL is public and the pipeline spends API credit:**

- `POST /documents` requires an `X-API-Key` header checked against an env var.
  GET endpoints stay public and read-only.
- Reject uploads over 10 MB, without a `%PDF` magic prefix, or over a sane page
  cap — before any byte reaches the pipeline.
- The queue subscriber and any cron path authenticate with a `CRON_SECRET`-style
  bearer check; they are not callable anonymously.
- Add a per-IP rate-limit rule on `POST /documents` in the Vercel Firewall.

---

## Phases

Each phase ends at a gate that produces a verifiable result. **Stop at each gate
and report. Do not start the next phase without being told.**

### Phase 0 — Scaffold
Repo structure, `uv` project, ruff + mypy (strict) + pytest, `Makefile` with
`make check`, `docs/dev-log.md`, `docs/decisions/`, `.env.example`, and a GitHub
Actions workflow running `make check` on every push.
**Gate:** `make check` passes clean on the empty project, locally and in CI.

### Phase 1 — Domain + persistence
Pydantic v2 canonical models, SQL migrations including the exclusion constraint,
`RecordRepository` port + psycopg adapter, docker-compose Postgres for tests.
**Gate:** migrations apply locally *and* on a Neon branch (`CREATE EXTENSION
btree_gist` succeeds; psycopg works through the pooled endpoint); a test proves
the database *rejects* an overlapping bracket insert; a test proves an open-ended
top bracket is accepted; a test proves a negative rate is accepted.

### Phase 2 — Extraction pipeline + accuracy harness
Router, all `TableExtractor` adapters, `SchemaMapper` adapters, the
`RecordVerifier` and `Adjudicator` (semantic-layer amendment above), validators,
confidence scoring, review queue. Build the accuracy harness **alongside** the
pipeline, not after.
**Gate:** field-level accuracy report printed as a table by document and by record
type, with a per-document verifier-disagreement column and cost itemized by role.
Target 128/128, through the mapper+verifier layer. Below that, name every failing
record and the reason.

### Phase 3 — API
FastAPI implementation of the surface above. Export OpenAPI 3.1 to
`docs/openapi.yaml`. Contract tests.
**Gate:** all endpoints tested; `tax_year=2026` excludes superseded records;
pagination stable across concurrent inserts.

### Phase 3.5 — Deploy to Vercel
Vercel adapters wired behind config, `vercel.json` (maxDuration, excludeFiles,
crons if the fallback is needed), `scripts/seed_remote.sh` that POSTs the five
fixtures to a deployed URL with the API key.
**Gate:** a preview deployment serves every endpoint; the seed script ingests all
five fixtures against the deployed URL; `GET /records?tax_year=2026` is correct
over the live URL; unauthenticated `POST` is rejected; first-hit latency (function
cold start + Neon resume) measured and recorded in the README. Promotion to
production is done by hand.

### Phase 4 — CDK stack
Python CDK v2. `cdk-nag` with `AwsSolutionsChecks`. Every suppression carries a
written justification.
**Gate:** `cdk synth` succeeds **with no AWS credentials present**; `cfn-lint`
clean on the synthesized template; zero unsuppressed cdk-nag errors; the
offline-synth + cfn-lint + cdk-nag sequence runs as a CI job on every push. Commit
`infra/cdk.out/`.

### Phase 5 — Documentation
C4 Level 1 (Context) and Level 3 (Components) in Mermaid. **Verify they render on
GitHub** — the native `C4Component` syntax is experimental and often does not; if
it fails, use `flowchart` with subgraphs applying C4 semantics correctly. README
with accuracy table, cost analysis, bottleneck section, three-targets deployment
notes. Consolidate ADRs.
**Gate:** diagrams render; README addresses all four evaluation criteria explicitly.

---

## Documentation requirements

**`docs/dev-log.md` is a deliverable.** Append as you work — what was tried, what
failed, what was chosen and rejected. The exercise explicitly asks for
documentation of development steps and tool choices. Include honestly how AI
assistance was used; for an AI Engineer role that is signal, not something to hide.

**ADRs** (one page each) for at least: database engine, Aurora DSQL rejection, Data
API rejection, Aurora Serverless v2 rejection with migration threshold, CDK over
Terraform, hybrid extraction router, polymorphic table over per-type tables,
Vercel as the live target, Queues-based JobRunner on request-scoped compute,
vision-OCR as the Vercel extractor for scanned input, blob-in-Postgres vs Vercel
Blob.

**The README must state plainly** that the CDK stack synthesizes and validates but
was not deployed (no AWS account), that the live URL runs the Vercel adapters, and
that `docker compose up` reproduces the full service locally from a fresh clone.
Never imply an AWS deployment that does not exist.

**The README must disclose that the five fixtures and the ground truth are
self-authored** — the brief supplied no documents. Turn this into a strength: a
"Fixture design" section documenting the five shapes and the deliberate traps as
test engineering.

**The bottleneck section must be quantitative.** Name what breaks first at 10,000
documents/day and how it is mitigated on both live targets: extraction API rate
limits, Lambda concurrency, Distributed Map `MaxConcurrency`, Queues subscriber
concurrency and `maxDuration` on Vercel, and connection exhaustion against
Postgres under fan-out — the reason RDS Proxy (AWS) and the pooled Neon endpoint
(Vercel) are in the design at all.

---

## Working style

- Small commits, conventional commit messages.
- Tests before or alongside implementation, never after.
- At a real design fork, stop and ask. Do not guess and build.
- Prefer boring, correct code over clever code.
- When a gate fails, report the failure. Never adjust the test.
- **Session discipline — three rules, and they exist because the hazard
  recurred within a day.** Two sessions on one working tree have no protocol
  between them. On 2026-08-26 a second session's `git add -A` swept another's
  five staged-but-uncommitted files into an unrelated commit; on 2026-08-27
  the same collision ran in the opposite direction, one session committing
  another's mid-edit tree as though it were finished work. Both are recorded
  in `docs/dev-log.md`.

  1. **One interactive session per repository.**
  2. **The previous session ENDS before the next one opens** — closed, not
     "winding down". Both incidents happened inside an overlap window
     everyone believed was already over.
  3. **Every operator prompt names its target session.** A prompt that is not
     addressed to me is evidence that another session is live, which is the
     only one of these three rules a session can enforce from the inside.

  Corollary, from incident #2: **a dirty working tree is a LIVE tree until a
  human says otherwise.** "It looks like finished work someone forgot to
  commit" is indistinguishable in `git status` from "mid-edit, two steps from
  done". Do not commit changes you did not make; stop and ask.

  `.fanout-active` and the separate `tax_test` database guard a session
  against its own background work; nothing guards two sessions against each
  other, and a lock would be the wrong instrument.
