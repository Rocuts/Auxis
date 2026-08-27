# Tax Table Ingestion Service

Accepts PDF documents containing tax tables, extracts the tabular data,
normalizes it into a canonical schema, persists it in PostgreSQL, and exposes
it over a REST API.

The claim this repository is organized around: **one domain, three deployment
targets, proven rather than asserted.** The same pipeline runs under
`docker compose`, on Vercel, and in an AWS CDK stack, because every boundary
that touches a platform is a port with three adapters behind it.

> **Status (2026-08-27, after production promotion).** The service is live at
> **https://auxis-johan-rocuts-projects.vercel.app** and every `GET` serves.
> Two results dominate this README, and both are reported at full weight
> including the parts that did not work:
>
> 1. **Accuracy is 81/128**, closed under a frozen specification after six
>    measured gate runs. The best run scored 119/128; it is recorded *beside*
>    the shipped number rather than replacing it, because two pre-registered
>    repair attempts both made the score worse and suppressing that would
>    turn a specification into a curve fit.
> 2. **The live database holds 113 of 128 records.** Document `05`, the
>    scanned one, is short **16** of its 19; document `04` carries one extra,
>    so the net gap is 15. It extracts **19 of 19 on the local
>    Tesseract path** (gate-measured) and **3 of 19 on Vercel's vision path**
>    (live-measured once, 2026-08-27) — a documented
>    [per-target limitation](#cost), fail-visible via 19 review-queue items
>    rather than a silent loss.
>
> **Getting there took three deployment defects that only a real deployment
> could surface**, and all three are fixed, test-first, and deployed:
>
> - a killed worker **stranded its job forever**, because the cron sweep
>   claimed only `queued` rows and nothing rewrote a row whose process was
>   gone — fixed with a lease/visibility timeout;
> - a retry then **duplicated records** (60 rows for a 32-record document),
>   because natural-key upsert assumes a deterministic producer and
>   [ADR 014 §8](docs/decisions/014-semantic-layer-model-selection.md)
>   measured that ours is not — fixed by making re-ingest a document-scoped
>   atomic *replace*;
> - the adjudication pass could **outrun its own budget**, because the
>   wall-clock check sat between items while one call could spend
>   `300 s × 4` SDK attempts — fixed by bounding the per-item ceiling.
>
> `maxDuration` was also re-derived from measured wall-clock (300 s → 1800 s)
> and the cron batch resized to match, both as conformance with this
> project's own sizing rule rather than new policy.
>
> The repairs landed **after** the gate that found them closed, never during
> it: a measurement gate whose subject is edited mid-measurement is not a
> measurement. Nothing in this document is estimated and then presented as
> measured; where something was measured, the raw figures and the date are
> given.

---

## Table of contents

- [The four evaluation criteria, and where each is answered](#the-four-evaluation-criteria-and-where-each-is-answered)
- [Quick start](#quick-start)
- [Architecture](#architecture)
  - [C4 Level 1 — System context](#c4-level-1--system-context)
  - [C4 Level 3 — Components](#c4-level-3--components)
  - [Ports and adapters](#ports-and-adapters)
  - [The extraction router](#the-extraction-router)
  - [The semantic layer](#the-semantic-layer)
  - [The data model](#the-data-model)
- [API surface](#api-surface)
- [Accuracy](#accuracy)
- [Defense in depth, demonstrated](#defense-in-depth-demonstrated)
- [Model selection](#model-selection-a-decision-with-a-rule-written-before-the-numbers)
- [Cost](#cost)
- [Parallel processing and bottlenecks](#parallel-processing-and-bottlenecks)
- [Fixture design](#fixture-design)
- [The three deployment targets](#the-three-deployment-targets)
- [Honest limitations](#honest-limitations)
- [Development log, decisions, and how AI was used](#development-log-decisions-and-how-ai-was-used)

---

## The four evaluation criteria, and where each is answered

| Criterion | Where |
|---|---|
| Clear backend and API design | [Architecture](#architecture), [API surface](#api-surface), [The data model](#the-data-model) — one canonical fact table, a database that makes overlapping brackets *unrepresentable*, cursor pagination, `202`-then-poll ingest |
| Practical approach to PDF extraction and normalization | [The extraction router](#the-extraction-router) and [The semantic layer](#the-semantic-layer) — deterministic first, models only where the task is judgment, and a review queue instead of a guess |
| Reasoning about parallel processing and bottleneck mitigation | [Parallel processing and bottlenecks](#parallel-processing-and-bottlenecks) — what breaks first at 10,000 documents/day, with the arithmetic and the knobs, on both live targets |
| Documentation of development steps and tool choices | [`docs/dev-log.md`](docs/dev-log.md), [`docs/decisions/`](docs/decisions/), and [`docs/audit/`](docs/audit/) — including an honest account of how AI assistance was used |

**The brief's own deliverable is met in full:** a live service that accepts a
tax-table PDF, extracts and normalizes it into one canonical schema, persists
it, and returns the stored records over `GET`. Every endpoint is implemented,
contract-tested, and exported to [`docs/openapi.yaml`](docs/openapi.yaml).

### What is beyond the brief, and why it is here

Two of the things this README spends the most words on **were not asked for**,
and it is worth saying so plainly rather than letting them read as scope the
brief demanded:

- **The accuracy harness** — field-level scoring of every extracted record
  against a ground truth, with a natural-key comparison, per-document and
  per-record-type breakdowns, and a conformance ledger.
- **The adversarial oracle** — five self-authored PDFs built to break specific
  naive assumptions, a documented `deliberate_traps` array, and an isolation
  rule (`src/` may never read the answers) enforced by a test rather than
  promised.

Neither is required to ship the service. Both exist because *"the extraction
works"* is a claim, and this project's position is that a claim about
extraction accuracy which cannot be falsified is not worth making. The cost of
that position is visible throughout: six measured runs, a best score of
119/128, a shipped score of 81/128, and two repair attempts that made the
number worse — all reported rather than smoothed. **A harness you cannot fail
is a harness that proves nothing**, and the failures below are what the rigor
bought.

The one thing the harness does *not* speak to is the live URL's own data. The
production seed was pre-declared as **one more declared draw of the frozen
specification** — the same prompts, the same models, the same `sha256`-pinned
conventions — not a fresh result and not a better one.

**The first attempt never became a draw at all** — all five jobs were killed
by the platform before reaching the comparison, an infrastructure failure
rather than a semantic one. After the fixes, the re-seed landed **113 of 128
records with zero duplicates**, and the distinction is worth keeping: 81/128
is what this pipeline *scores* against the oracle, 113/128 is what this
*deployment* currently *holds*, and they are not the same measurement. The
15-record gap is document 05's vision extraction, not a mapping failure.
Details in
[Parallel processing and bottlenecks](#parallel-processing-and-bottlenecks).

---

## Quick start

```bash
cp .env.example .env
docker compose up -d --wait db
uv sync
```

Then edit `.env`. Three things, and the third is the one that is easy to
miss:

1. **`DATABASE_URL=postgresql://tax:tax@localhost:5433/tax`** — the
   credentials and the 5433 port are what `docker-compose.yml` publishes. The
   shipped value is a Neon-shaped placeholder; it will not reach the
   container.
2. **`API_KEY`** and **`CRON_SECRET`** — any non-empty values. They are yours;
   the curls below just have to agree with them.
3. **Uncomment the `AS SHIPPED` block** — `SCHEMA_MAPPER_API_KEY`,
   `SCHEMA_MAPPER_BASE_URL`, `SCHEMA_MAPPER_MODEL`, and **both**
   `SCHEMA_MAPPER_USD_PER_MTOK_*` prices — plus the three `RECORD_VERIFIER`
   lines (`RECORD_VERIFIER_MODEL` and **both** its prices), and set your AI
   Gateway key on `SCHEMA_MAPPER_API_KEY`. A model without its prices is
   reported at the Opus defaults, which is why the prices travel with it.
   Skip this and the API still runs; the pipeline fails at
   `missing_credentials`.

```bash
export $(grep -E '^DATABASE_URL=' .env)   # migrate reads shell env, not .env
uv run python -m tax_tables.migrate
make api                                  # http://localhost:8000/docs
```

**Ingesting a document locally takes one more call, and that is by design.**
`JOB_RUNNER` defaults to `none`, so an upload is *enqueued* and returns `202`
without starting work — there is no resident worker in this tree, and the
compose file runs Postgres only. Start the work explicitly:

```bash
curl -X POST localhost:8000/documents \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/pdf" \
  -H "x-filename: 01_federal_income_tax_rate_schedules_TY2026.pdf" \
  --data-binary @fixtures/01_federal_income_tax_rate_schedules_TY2026.pdf

curl -X POST "localhost:8000/internal/sweep?limit=1" \
  -H "Authorization: Bearer $CRON_SECRET"     # runs the pipeline, blocking

curl "localhost:8000/records?limit=5"
```

**If `items` comes back empty, ask the job why:** `GET /jobs/{job_id}` with the
id the upload returned. `error.type = "missing_credentials"` means the mapper
key is unset — step 3 above.

The sweep needs a funded model key (`SCHEMA_MAPPER_API_KEY`, or
`ANTHROPIC_API_KEY` to go direct); without one the job reaches a terminal
`failed` with `error.type = "missing_credentials"`, which is the honest
outcome rather than a hung request. Setting `JOB_RUNNER=vercel` is what makes
the upload self-kick instead, and that is what the deployed target runs.

Run everything the project can verify without credentials:

```bash
make check       # ruff + mypy --strict + pytest (705 passed, 1 skipped)
make synth-check # cdk synth with NO AWS credentials, then cfn-lint
make diagrams    # every README mermaid block, under two Mermaid majors
```

`make accuracy` runs the end-to-end accuracy gate against real models and is
the one skipped test above. It reads `.env` and needs a funded key on
whichever route you configure: `SCHEMA_MAPPER_API_KEY` with
`SCHEMA_MAPPER_BASE_URL` for the Vercel AI Gateway (what every measured run
here used), or `ANTHROPIC_API_KEY` alone to go direct to `api.anthropic.com`.
The gateway route is the one `.env.example` documents as shipped.

The four scripts, since each answers a question the test suite cannot:

| Script | What it is for |
|---|---|
| [`scripts/seed_remote.sh`](scripts/seed_remote.sh) | POST the five fixtures to a deployed URL and poll every job to a terminal state. Non-zero exit if any upload is rejected or any job fails — a gate, not a demo. **Set `POLL_SECONDS`** (see [below](#reproducing-the-seed-against-a-deployment)) |
| [`scripts/probe_transport.py`](scripts/probe_transport.py) | Two probes run *before* a paid gate: is this key entitled to this model, and does the transport **enforce** a structured-output contract or merely forward it? The second question is the one that cost this project a run to learn ([ADR 014 §8j](docs/decisions/014-semantic-layer-model-selection.md)) |
| [`scripts/mark_stranded_jobs.py`](scripts/mark_stranded_jobs.py) | Close the five jobs the 3.5-LIVE gate stranded in `running` to `failed`, with an error payload naming the gate. Idempotent, targeted by id, guarded on status. **Marks, never deletes** — the rows are the evidence |
| [`scripts/check_diagrams.py`](scripts/check_diagrams.py) | Parse every README mermaid block under Mermaid 10 **and** 11, because "it renders in my editor" proves nothing about GitHub |

---

## Architecture

### C4 Level 1 — System context

```mermaid
flowchart TB
    classDef person fill:#08427b,stroke:#052e56,color:#ffffff
    classDef core fill:#1168bd,stroke:#0b4884,color:#ffffff
    classDef ext fill:#8b8b8b,stroke:#5f5f5f,color:#ffffff
    classDef designed fill:#8b8b8b,stroke:#5f5f5f,color:#ffffff,stroke-dasharray: 5 5

    consumer["API consumer<br/>[Person]<br/>Uploads tax-table PDFs,<br/>queries canonical records"]
    reviewer["Data reviewer<br/>[Person]<br/>Resolves what the pipeline<br/>refused to guess"]

    subgraph scope["System scope"]
        svc["Tax Table Ingestion Service<br/>[Software System]<br/>Extracts tabular data from PDFs,<br/>normalizes it to one canonical schema,<br/>persists it, and serves it"]
    end

    llm["Model API<br/>[External System]<br/>Anthropic Messages protocol<br/>live: AI Gateway - GLM + qwen<br/>by config: direct Anthropic / Bedrock"]
    aws["AWS Textract and Bedrock<br/>[External System - designed only]<br/>The AWS target's extraction<br/>and model transport"]
    db[("PostgreSQL 18<br/>[External System]<br/>Neon, RDS, or local container")]

    consumer -->|"POST /documents with X-API-Key,<br/>GET /records - HTTPS/JSON"| svc
    svc -->|"Mapping, verification, adjudication;<br/>vision OCR for scanned pages<br/>HTTPS/JSON"| llm
    svc -.->|"Same ports, AWS adapters<br/>synthesized but never deployed"| aws
    svc -->|"Records, jobs, review queue<br/>TCP/TLS"| db
    reviewer -->|"GET /reviews - read-only;<br/>resolving is not an HTTP action"| svc

    class consumer,reviewer person
    class svc core
    class llm,db ext
    class aws designed
```

Two things in that picture are deliberately uncomfortable and are stated
rather than hidden. The AWS system is dashed because it was **designed and
validated but never deployed**. And the reviewer's arrow is one-way: they can
*read* the queue over HTTP, but resolving an item is a human judgment this API
does not accept — the write half is out of scope by decision, not by omission
(see [Honest limitations](#honest-limitations)).

### C4 Level 3 — Components

Components inside the service, with the ports drawn as boundaries. Every
component below is platform-agnostic; only the adapters named in the dashed
boxes change between targets.

```mermaid
flowchart TB
    classDef comp fill:#438dd5,stroke:#2e6295,color:#ffffff
    classDef port fill:#1168bd,stroke:#0b4884,color:#ffffff
    classDef adapter fill:#b0b0b0,stroke:#7a7a7a,color:#111111
    classDef store fill:#8b8b8b,stroke:#5f5f5f,color:#ffffff

    subgraph api_b["Write path - FastAPI"]
        guards["Upload guards<br/>[Component: FastAPI]<br/>X-API-Key, 10 MB cap,<br/>%PDF magic, page cap -<br/>all before the first byte<br/>reaches the pipeline"]
        registrar["Document registrar<br/>[Component: psycopg]<br/>SHA-256 is the natural key,<br/>so re-upload is a no-op"]
        enqueue["Job enqueuer<br/>[Component: psycopg]<br/>One live job per document,<br/>enforced by a partial index"]
    end

    subgraph read_b["Read path - FastAPI"]
        queries["Query layer<br/>[Component: SQL]<br/>Filters, cursor pagination,<br/>bracket resolve"]
    end

    subgraph runner_b["JobRunner port"]
        runner["JobRunner<br/>[Port]<br/>Accepted work becomes<br/>running work, and a job<br/>whose worker was killed is<br/>reclaimed on lease expiry"]
        runner_a["in-process pool - local<br/>cron sweep - Vercel<br/>Distributed Map - AWS"]
    end

    subgraph pipe_b["Pipeline - run_document"]
        router["ExtractionRouter<br/>[Component]<br/>Classifies every page on raw<br/>evidence: text layer, page image,<br/>orientation. Never on filename"]
        extractor["TableExtractor<br/>[Port]<br/>Every page goes through here.<br/>Its adapters are the only<br/>components licensed to read pixels"]
        extractor_a["pdfplumber - digital, $0<br/>Tesseract - local OCR<br/>vision-OCR - Vercel<br/>Textract - AWS"]
        mapper["SchemaMapper<br/>[Port]<br/>Cell grid to records.<br/>Semantic mapping only:<br/>never reads a number<br/>off an image"]
        verifier["RecordVerifier<br/>[Port]<br/>Re-derives in a fresh<br/>context under a skeptic<br/>prompt. Flags, never corrects"]
        triage["Validators and triage<br/>[Component]<br/>Confidence floor, bracket<br/>rules, unit checks"]
        adjudicator["Adjudicator<br/>[Port]<br/>One pass over open queue<br/>items with full evidence"]
        semantic_a["Anthropic Messages protocol<br/>live: AI Gateway - local, Vercel<br/>mapper + adjudicator: zai/glm-5.3-flash<br/>verifier: alibaba/qwen-3-235b<br/>by config: direct Anthropic / Bedrock - AWS<br/>same prompts, same parsers"]
    end

    subgraph repo_b["RecordRepository port"]
        repo["RecordRepository<br/>[Port]<br/>Persist, queue, resolve"]
        repo_a["psycopg to Neon,<br/>RDS Proxy, or container"]
    end

    db[("PostgreSQL<br/>[Container]<br/>records with a GiST exclusion<br/>constraint, documents,<br/>document_blobs, jobs,<br/>review_queue")]

    guards --> registrar --> enqueue --> runner
    runner --> router
    router -->|"page needs pixels"| extractor
    router -->|"page has a usable text layer"| extractor
    extractor --> mapper
    mapper --> verifier --> triage
    triage -->|"persistable records"| repo
    triage -->|"findings and mapper issues"| repo
    repo --> adjudicator
    adjudicator -->|"resolution with citations,<br/>or a proposal for a human"| repo
    repo --> db
    queries --> db

    extractor -.-> extractor_a
    mapper -.-> semantic_a
    verifier -.-> semantic_a
    adjudicator -.-> semantic_a
    runner -.-> runner_a
    repo -.-> repo_a

    class guards,registrar,enqueue,queries,router,triage comp
    class runner,extractor,mapper,verifier,adjudicator,repo port
    class runner_a,extractor_a,semantic_a,repo_a adapter
    class db store
```

> **Why these are `flowchart`s and not `C4Component`.** Mermaid's C4 syntax is
> a plugin, and GitHub's bundled Mermaid does not include it — C4 blocks render
> as raw text there while rendering perfectly in the Mermaid live editor and in
> `mermaid-cli` ([GitHub community discussion #197898](https://github.com/orgs/community/discussions/197898),
> closed unanswered, 2026-06-03). "It renders locally" therefore proves nothing
> about GitHub. These diagrams apply C4 semantics through subgraph boundaries
> and typed labels instead, and `make diagrams` parses every block under
> Mermaid 10 **and** 11 to bracket whichever version GitHub ships.

### Ports and adapters

The domain and the pipeline do not know that AWS or Vercel exist.

| Port | local (`docker compose`) | Vercel (live target) | AWS (designed, synth-only) |
|---|---|---|---|
| `TableExtractor` | pdfplumber / Tesseract | pdfplumber / vision-OCR | pdfplumber / Textract |
| `SchemaMapper` | AI Gateway · `zai/glm-5.3-flash` | AI Gateway · `zai/glm-5.3-flash` | Bedrock |
| `RecordVerifier` | AI Gateway · `alibaba/qwen-3-235b` | AI Gateway · `alibaba/qwen-3-235b` | Bedrock |
| `Adjudicator` | AI Gateway · inherits the mapper | AI Gateway · inherits the mapper | Bedrock |
| `JobRunner` | no-op runner + explicit sweep | self-kick + cron sweep | Step Functions Distributed Map |
| `RecordRepository` | psycopg → container | psycopg → Neon pooled | psycopg → RDS Proxy |
| `BlobStore` | Postgres `bytea` | Postgres `bytea` | S3 |

All three semantic roles speak the **Anthropic Messages protocol**; which
endpoint answers it is configuration. The live and local route is the **Vercel
AI Gateway** (`ai-gateway.vercel.sh`), serving `zai/glm-5.3-flash` to the
mapper and adjudicator and `alibaba/qwen-3-235b` to the verifier. **Direct
Anthropic** (`api.anthropic.com`) and **Bedrock** (AWS, designed-only) are the
other two config-selected routes: both are wired and neither is funded here —
no direct-Anthropic key is provisioned on this project, and every measured run
in this README went through the gateway.

The Bedrock adapters are the cheapest possible proof that the seam is real:
they inject `AnthropicBedrock` into the *same three adapter classes*. Zero
duplicated prompts, schemas, parsers, or cost arithmetic — and a test
AST-parses the CDK stack so that a model-id drift between infrastructure and
application fails loudly.

### The extraction router

**Four of the five documents have a text layer.** Running a paid
table-extraction service on them is waste: `pdfplumber` reads the PDF's actual
table structure with higher fidelity than any model inferring it from pixels,
at zero cost, reproducibly.

```
page has a usable text layer  ->  deterministic extraction (pdfplumber). $0.
page is a scan or sideways    ->  the target's OCR adapter
page is genuinely blank       ->  nothing extracted, nothing spent
```

Classification is per page and on raw evidence — deduplicated upright
character count, presence of a page-sized image, text orientation — never on
the filename or document metadata. Two invariants are pinned by tests, one in
each direction:

- a page with a usable text layer is **never** sent to an OCR adapter (OCR
  costs money on two of three targets);
- a page dominated by a page-sized image is **never** handed to the
  deterministic adapter, even when it also carries a small text layer. A
  scanner stamp of 60 characters would otherwise classify a whole scanned page
  as digital, `pdfplumber` would find no tables, and the document would come
  back *empty at confidence 1.0*. That is anti-goal #8's silent loss, and it
  was found by adversarial review and reproduced with a stamped-scan probe.

### The semantic layer

Three model roles, each single-pass, each justified against Anthropic's
published criteria for adding agents ([ADR 012](docs/decisions/012-runtime-multi-agent-semantic-layer.md)):

- **`SchemaMapper`** receives an already-extracted cell grid and decides what
  each cell *means* — which column is a rate, which is a bound, which filing
  status a column belongs to, whether a dash means null. It never reads a
  number off an image and never invents a value.
- **`RecordVerifier`** re-derives the same records in its own context window,
  under a skeptic prompt, with no access to the mapper's reasoning. Context
  isolation is the whole point: two independent derivations that agree is
  evidence; a model agreeing with its own transcript is an echo. A dispute is
  a **flag, never a correction** — the record persists as `needs_review` and
  the dispute reaches the queue with its reason.
- **`Adjudicator`** makes one pass over open queue items. Above a confidence
  threshold, with citations validated against the extracted document, the item
  auto-resolves with a full audit trail; below it, the proposal is stored and
  the item stays with a human. Auto-resolution applies **only** to items whose
  record actually persisted: a queue row standing for data the fact table
  refused is the only live signal of that loss, and never auto-closes.

All three speak the **Anthropic Messages protocol**, and the endpoint is
configuration — live and local, that is the **Vercel AI Gateway**:
`zai/glm-5.3-flash` for the mapper and adjudicator, `alibaba/qwen-3-235b` for
the verifier, a deliberately different family ([ADR
014](docs/decisions/014-semantic-layer-model-selection.md)). Direct Anthropic
and Bedrock are the other two config-selected routes, wired but unfunded here.

Everything else stays deterministic on purpose. The first published criterion
is to find the simplest thing that works, and for structural extraction that
is not a model. An agent in the router would add nondeterminism, latency, and
per-document token cost with no accuracy left to buy.

### The data model

One canonical fact table: a typed core (provenance, temporal validity,
jurisdiction, a `record_type` discriminator, value slots, confidence, review
status) plus a JSONB tail for type-specific attributes. Not eleven tables —
every new document shape would need a migration. Not one blob — no
constraints, no type safety.

**The centerpiece** is that overlapping brackets are not validated in
application code; they are *unrepresentable*:

```sql
EXCLUDE USING gist (
    jurisdiction                   WITH =,
    record_type                    WITH =,
    tax_year                       WITH =,
    (COALESCE(filing_status, ''))  WITH =,
    (COALESCE(taxpayer_class, '')) WITH =,
    lifecycle_status               WITH =,
    bracket                        WITH &&
) WHERE (bracket IS NOT NULL)
```

Three facts about the domain that a naive schema gets wrong, each with a test:

- the top bracket is **open-ended** (`and over`) — `int8range` with a null
  upper bound, accepted;
- one local rate in document 03 is **legitimately negative** (a statutory
  rebate) — a `rate >= 0` check would reject valid data;
- a long dash means **no tax imposed** — `NULL`, not zero.

Gap-freeness cannot be an exclusion constraint (it is a cross-row aggregate),
so it lives in a validation step and a diagnostic view.

Idempotency at two levels: SHA-256 of the document bytes is its natural key,
so re-uploading the same PDF is a no-op; and records carry a
`UNIQUE NULLS NOT DISTINCT` natural key.

---

## API surface

`docs/openapi.yaml` is exported from the app and a contract test fails if it
goes stale.

| Method | Path | Auth | Notes |
|---|---|---|---|
| `POST` | `/documents` | `X-API-Key` | **202 + `job_id`.** Never blocks on extraction |
| `GET` | `/jobs/{job_id}` | public | status, counts, error payload |
| `GET` | `/records` | public | `tax_year`, `jurisdiction`, `record_type`, `filing_status`, `effective_on`, `include_superseded`, `min_confidence`; **cursor** pagination |
| `GET` | `/records/resolve` | public | the bracket containing an amount |
| `GET` | `/documents`, `/documents/{id}` | public | provenance |
| `GET` | `/reviews` | public | everything the pipeline refused to guess; filters `status`, `document_id`; **cursor** pagination |
| `GET` | `/reviews/{id}` | public | one item with its adjudication audit trail |
| `POST` `GET` | `/internal/sweep` | `CRON_SECRET` bearer | the sweep path, registered under both methods with distinct operation ids: `POST` for the self-kick, `GET` because that is what a Vercel cron issues. `?limit=` bounds the batch, and the batch runs **sequentially in one invocation** — which is why `limit` is coupled to `maxDuration` ([ADR 009](docs/decisions/009-cron-sweep-jobrunner.md)) |

`GET /records?tax_year=2026` returns active 2026 records and excludes
superseded ones. That is a test, not a claim — and the distinction between
the test and the live URL is worth one paragraph, because they do not
currently agree.

> **The superseded filter is proven by the suite and NOT yet demonstrable
> live.** The test seeds a `superseded` record and proves it is filtered; that
> is where the property is held. Over the live URL the query is *vacuously*
> satisfied: all 113 live records are `active`, so there is nothing to
> exclude. The reason is document 05 — the superseded document — under-
> extracting on Vercel's vision path, so it never contributed the superseded
> rows the check needs. **Recorded as open, not as passed.** It closes when
> the vision path does.
>
> The same query returns **59 of the 113 live records**, and the shortfall is
> the identical failure shape: every `ordinary_income_bracket`,
> `standard_deduction`, `surtax_threshold`, `wage_base`,
> `withholding_allowance` and `employment_tax_rate` record carries
> `tax_year = 2026`, while all 51 `sales_tax_rate` rows (document 03) and all
> 3 `special_gain_rate` rows (document 05) carry **null** — a field the model
> did not emit, never a value it got wrong.

`GET /records/resolve` returns the applicable bracket **record**. It is a data
lookup, and the response deliberately does not read as a computed tax
liability. Live example against the deployed URL:

```
GET /records/resolve?amount=150000&filing_status=single
      &taxpayer_class=individual&tax_year=2026&jurisdiction=US-FED
-> the 106151-202650 bracket at 0.24, active, with its page and table
```

**`taxpayer_class=individual` is not decoration, and that is a finding.** An
earlier run of the same document under the same frozen spec emitted those
records with `taxpayer_class` null, and this one emitted `individual`. Both
are in the natural key, so *which query answers depends on which draw
persisted*. [ADR 014 §8](docs/decisions/014-semantic-layer-model-selection.md)
measured that stochasticity in the accuracy table; here it is **visible in the
API contract itself**, which is a more expensive place for it to live.

(`jurisdiction` defaults to `US-FED`. It shipped as `US`, which the canonical
vocabulary never produces — so the default could not match a federal record,
and the first hand-written call against production 404'd over a database
holding exactly the bracket it asked for. Found live, fixed, and pinned by a
test that reads the published schema.)

The review surface is **read-only, and the schema pins it that way** — a
contract test asserts that `/reviews` and `/reviews/{id}` expose `get` and
nothing else, so "no write path" survives a future edit that adds a handler
without revisiting the decision.

---

## Accuracy

> **Gate 2b CLOSED at 81/128 — the sixth run was pre-registered as final, and
> it is the worst of the three that mattered.** The best result this project
> measured was the fourth run's **119/128**. Two attempts to repair the fifth
> run's regression made it worse instead, and the honest close is to report
> that rather than to keep tuning. Every failing record is named below, and
> **no mapped value was wrong in any of the six runs** — every failure is a
> field the specification asked for and did not describe well enough to get.

| Document | Expected | Correct | Field-diff | Missing | Spurious | Verifier disputes |
|---|---|---|---|---|---|---|
| `01_federal_income_tax_rate_schedules_TY2026.pdf` | 32 | 4 | 0 | 28 | 28 | 1 |
| `02_standard_deduction_schedule_TY2026.pdf` | 8 | **8** | 0 | 0 | 0 | 3 |
| `03_state_local_sales_tax_rates_2026.pdf` | 51 | **51** | 0 | 0 | 0 | 0 |
| `04_employment_tax_rates_and_thresholds_2026.pdf` | 18 | **18** | 0 | 0 | 0 | 0 |
| `05_capital_gains_preferential_rates_TY2025.pdf` | 19 | 0 | 19 | 0 | 0 | 5 |
| **Total** | **128** | **81** | **19** | **28** | **28** | **9** |

| Record type | Gate 4 | Gate 5 | **Gate 6** |
|---|---|---|---|
| `ordinary_income_bracket` | 32/32 | 4/32 | **4/32** |
| `preferential_gain_bracket` | 12/12 | 12/12 | **0/12** |
| `special_gain_rate` | 3/3 | 3/3 | **0/3** |
| `surtax_threshold` | 4/9 | 9/9 | **5/9** |
| `wage_base` | 0/3 | 3/3 | **3/3** |
| `employment_tax_rate` | 3/4 | 4/4 | **4/4** |
| `sales_tax_rate` | 51/51 | 51/51 | **51/51** |
| `standard_deduction` | 5/5 | 5/5 | **5/5** |
| `additional_standard_deduction` | 2/2 | 2/2 | **2/2** |
| `dependent_deduction_rule` | 1/1 | 1/1 | **1/1** |
| `withholding_allowance` | 6/6 | 6/6 | **6/6** |
| **Total** | **119/128** | **100/128** | **81/128** |

**The 47 failures, named, in two groups.**

*Group 1 — 28 missing, document 01, `ordinary_income_bracket`:*

> `US-FED | 2026 | <filing_status> | taxpayer_class` — **expected
> `individual`, got `null`**, across 7 bracket rows x 4 filing statuses. The
> same document's 4 `estate_or_trust` records matched and scored. Every other
> field on all 28 is correct; they fail on the natural key alone, which is why
> they show as `Missing` and `Spurious` rather than `Field-diff`.

*Group 2 — 19 field mismatches, document 05, every record it has:*

> **All 19** are missing `superseded_effective` (expected `2026-01-01`). Three
> `special_gain_rate` records are additionally missing `max_rate` (`0.25`,
> `0.28`, `null`) and four `surtax_threshold` records are additionally missing
> `threshold` (`200000`, `200000`, `250000`, `125000`). 26 differing fields
> over 1,250 compared. **Not one is a wrong value** — every difference is
> `actual <absent>`, a key the model did not emit.

### The blast radius of the final revert, measured

The sixth run existed to undo the fifth run's regression by returning two
record-shape bullets to the text that scored 119/128. Both of its
pre-registered predictions ([ADR 014
§8g](docs/decisions/014-semantic-layer-model-selection.md)) were **wrong**,
and the two ways they were wrong are different:

| Prediction | Result | Reading |
|---|---|---|
| `ordinary_income_bracket` 4/32 → **32/32** | **4/32 — no change** | Neither deleted clause was the cause of the fifth run's 28-record regression. That causal story, and the competing hypothesis the pre-run audit raised against it, are **both refuted by measurement.** The true cause is still unidentified. |
| `preferential_gain_bracket` 12/12 → **12/12** | **0/12** | The revert cost this record type entirely — and took `special_gain_rate` (3/3 → 0/3) and document 05's four `surtax_threshold` records with it. |

The second row is the one that matters, and the reason is exact. Restoring the
`preferential_gain_bracket` bullet to its gate-4 wording deleted the sentence
*"Carries superseded_effective when its document is superseded."* That deletion
was justified — by us, in writing, before the run — on the grounds that it was
**information-free**, because the closed attribute dictionary states
`preferential_gain_bracket: superseded_effective` and that entry is
byte-identical at gate 4 and now.

The measurement says otherwise. **All 19 of document 05's records now omit
`superseded_effective`**, and at gate 4 — same bullet, same dictionary entry —
all 19 carried it. A rule stated identically in the authoritative dictionary
was sufficient then and is not sufficient now, because *everything around it
changed in between.*

Document 04 was the one prediction that held: 18/18, exactly as §8g argued,
because that document contains no records of either edited type.

### A prompt is read as a whole — the most transferable thing this project produced

This one is worth lifting out of the run report, because it generalises past
tax tables and past this codebase.

**The provenance, in order.** The reconciliation went under a three-lens
adversarial audit *before* the paid run — contradiction, completeness,
regression — plus an arbitrator that re-verified every quoted line against
the tree. **Two of the three lenses independently named this exact field, on
these exact 28 records, as their top finding.** The arbitrator refuted them,
and its argument was a good one: the `taxpayer_class` rule they were worried
about *had not changed since the run where document 01 scored 32/32*. Text
that is byte-identical to text that passed cannot be the cause of a new
failure. That is sound reasoning, and it was accepted.

It was also wrong, for a reason the arbitration had no way to see from the
text it was quoting: **the diff had changed the text's neighbourhood.** Two
bullets away, a new emphatic clause — *"but by filing status ONLY:
taxpayer_class is null on this record type"* — had been added to the
`preferential_gain_bracket` rule, in a register none of the surrounding
prose used. The model read the section as a whole and generalised the
emphasis to the adjacent `ordinary_income_bracket` rule, nulling
`taxpayer_class` on all 28 filing-status rows.

> **Unchanged text is only stable while its neighbourhood is unchanged.**
> A prompt is not a set of independent clauses that can be diffed one at a
> time. It is a document, read whole, and an adjacent sentence in a stronger
> register is a change to every rule near it — even when their bytes are
> identical.

**The sharpest detail is what the clause was defending.** It was added to
protect 12 `preferential_gain_bracket` records from taking a
`taxpayer_class` they should not have. But the field-level bullet, forty
lines above, already said so **by name**:

> `taxpayer_class` … is set on ORDINARY_INCOME_BRACKET RECORDS ONLY … on
> every other record_type — **including preferential_gain_bracket, which does
> carry a filing_status** — taxpayer_class is null.

That sentence is byte-identical in the 39/128, 119/128 and 100/128 runs, and
`preferential_gain_bracket` scored **12/12 in every one of them**. The
protection against *that* leak was already in place, already explicit, already
named, and already measured. The added clause bought nothing on the risk it
named and cost 28 records elsewhere — a restatement that was not merely
redundant but was itself the defect. *A second statement of a rule you have
already stated is not free; it is a change in emphasis, and emphasis is
exactly what a model generalises.*

> **Read this passage with the sixth run in hand, though.** The clause was
> removed, and `ordinary_income_bracket` did **not** recover — so the
> restatement was not the cause of the 28 after all, and the paragraph above
> is an argument this project believed, acted on, and then falsified with a
> measurement. What survives it is the general principle, not this particular
> attribution. The `12/12` streak ended at the same run, for an unrelated
> reason, in [the blast-radius table](#the-blast-radius-of-the-final-revert-measured).

**What we would do differently**, stated as a rule rather than a regret: an
adversarial reviewer that clears a finding on "this text is unchanged" must be
required to check what moved *next to* it. Stability is a property of a
neighbourhood, not of a line.

#### The third occurrence — where the lesson cost the most, and we caused it

The rule above was written into this README *before* the sixth run. The sixth
run then broke it, in the one direction nobody had guarded.

The revert restored two bullets to their gate-4 wording. One deletion it made
was defended in writing, in advance, as **information-free**: the sentence
*"Carries superseded_effective when its document is superseded"* duplicated an
entry the closed attribute dictionary already carried, and that entry is
byte-identical at gate 4 and today. The reasoning was that a rule stated twice
loses nothing by being stated once — the same reasoning that had just been
vindicated when a redundant restatement cost 28 records.

**It cost all 19 of document 05's records.** At gate 4 the dictionary entry
alone was enough; at gate 6, with the same bullet and the same entry, it was
not. Everything *around* both had been rewritten in between.

So the lesson is not "redundancy is dangerous". It is stricter and less
comfortable than that:

> **Text has no fixed meaning to a model — only a meaning in place.** Removing
> a restatement is as much a change as adding one, and *restoring* text to a
> wording that once worked does not restore its behaviour if its neighbourhood
> has moved. "This is byte-identical to a version that scored 32/32" is not a
> safety argument. It was not one when an arbitrator used it before gate 5, and
> it was not one when we used it before gate 6.

Three occurrences, three directions: an *added* emphasis generalising to its
neighbour (gate 5, −28), a *removed* redundancy that turned out to be
load-bearing (gate 6, −19), and a clause we *kept* whose stated justification
was simply false (§8f, caught by audit before it could cost anything). The
first was inherited. The second was ours, made while writing this section. We
are reporting it in the same document that argues the principle, because a
lesson a project learns and then violates is worth more evidence than one it
only recites.

### How it got here: six runs, and what each one measured

| | Baseline | Hardened | Gapped | **Reconciled-1** | Frozen | Reverted |
|---|---|---|---|---|---|---|
| Records delivered | 0 | 128 | 128 | 128 | 128 | 128 |
| mapper `item_ok` | 27.1% | 100% | 100% | 100% | 100% | 100% |
| verifier `call_ok` | 0% | 100% | 33.3% | 100% | 100% | 100% |
| Records flagged unverified | 128 | 0 | 50 | 0 | 0 | 0 |
| Natural keys matching | 0 | 0 | 124 | **128** | 100 | 100 |
| Fields compared | 0 | 0 | 1,562 | 1,614 | 1,250 | 1,250 |
| Fields **differing** | — | — | 255 | 11 | **0** | 26 |
| Verifier disputes | — | — | 0 | 9 | 2 | 9 |
| Cost, USD | — | — | — | 0.0428 | 0.0401 | 0.0424 |
| **Field-level accuracy** | **0/128** | **0/128** | **39/128** | **119/128** | **100/128** | **81/128** |

**The fourth run is the high-water mark, and the last two runs are recorded
beside it rather than in place of it.** That ordering is the point: a project
that reports only its best number has no way to distinguish a specification
from a curve fit. Runs five and six were both attempts to repair a regression,
both were made under a frozen spec and a pre-registered rule, and both made the
number worse. They are the evidence that the method was applied honestly, and
they are also the evidence that it did not converge.

Each run moved the failure one layer outward, and **every layer was a defect in
the specification, not in the model** — transport framing, then the identity
vocabulary, then bound semantics and an unnamed attribute tail, then one
unreconciled paragraph, and finally one over-eager sentence in the fix for
that paragraph. The harness drove specification completion in five steps, and
the fifth is the one that shows the method has teeth: it caught a regression
that a three-agent adversarial review had talked itself out of.

> **The closing fact of the progression: across all six runs, no mapped value
> was ever wrong.** Over 1,250 fields compared in the final run, 26 differ and
> **every one of them is `actual <absent>`** — a key the model did not emit,
> never a figure it got wrong. Every failure at every stage was a field the
> specification asked for and did not describe well enough to get: a framing
> convention, an identity vocabulary, a bound semantics, an attribute name, a
> contradiction between two paragraphs, a discriminator nulled by an adjacent
> sentence, an attribute lost when its restatement was deleted. The single
> value error in the entire exercise was document 05's `566751`, in the third
> run, which no later run reproduced.
>
> That is the honest summary of what an LLM did well here and what it did not.
> It read these documents correctly and consistently. What it could not do was
> guess a specification nobody had written down — and the harness is what
> turned each of those gaps from an opinion into a measurement.

Values were never copied from the oracle. What the conventions adopted from
the target schema are *encodings* — `US-FED`, `estate_or_trust`, the
`attribute_key` slugs, the attribute key names, and the inclusive-bounds
reading of `Over $X` — none of which is printed in any PDF, while every value
beneath them is read from the page. `src/` never opens
`fixtures/ground_truth.json`; the packaging excludes it from the deployed
bundle, and a test enforces both.

### What the independent verifier was actually worth

[ADR 012](docs/decisions/012-runtime-multi-agent-semantic-layer.md) argues
that a second model reviewing its own family's output is an echo, and puts a
*different* family (`alibaba/qwen-3-235b`) on the mapper's
(`zai/glm-5.3-flash`) work in a fresh context under a skeptic prompt — both
through the AI Gateway, one protocol, two vendors. Six runs give that
argument a measured answer rather than a theoretical one.

**It named the final run's real failures before the oracle was consulted, and
it did so twice across two different regressions.** In the fourth run, five of
nine disputes landed on exactly the `additional_medicare` records the harness
later scored wrong. In the sixth, five of nine landed on document 05 and named
the precise missing attributes:

> *"the record ... fails to include the extra attr `max_rate` as required by
> the schema for special_gain_rate records"* — and, on four `surtax_threshold`
> records, *"lacks the required extra attr `threshold`"*.

Those are, exactly, 5 of the 26 differing fields the harness found afterwards.
A model that had never seen the mapper's reasoning, working from the same
grid, re-derived the records and found the defect — the one thing
cross-family verification exists to do, measured rather than asserted.

Reported honestly, it cried wolf four times in the sixth run too. Three
disputes are against document 02, which scored **8/8**; two of the three
visibly argue themselves out of their own position and file the dispute
anyway (*"…so this is actually correct. Re-evaluating: no dispute … Therefore,
confirmed."*), and the third objects to a provenance citation naming one
prose index rather than another. The fourth is the recurring document 01
`257250` / `257300` dispute; this run cannot adjudicate it, because those
records failed on the natural key and never reached field comparison.

Four false positives cost four review-queue rows on correct records — a false
positive behaving exactly as a review queue is designed to absorb. The failure
worth fearing is the false *negative*, and a second family is what buys
protection against it.

**The limit of that protection, stated plainly.** `CANONICAL_CONVENTIONS` is
concatenated verbatim into the mapper's, the verifier's *and* the
adjudicator's system prompts. When the defect is in the conventions rather
than in a value, both models are reading the same law, and independence buys
nothing: the fifth run's 28-record regression drew **one** dispute. ADR 012's
context isolation is real for values and absent for conventions — worth
knowing before trusting a clean verifier column.

No dispute was settled by the models talking to each other. All nine became
review-queue flags for a human, which is the bound
[ADR 012](docs/decisions/012-runtime-multi-agent-semantic-layer.md) sets.

A per-record-type breakdown prints alongside the table. The **verifier
disagreement** column is deliberately not folded into the accuracy number: it
measures how much friction independent re-derivation produces, which is a
different question from whether the mapper was right.

---

## Defense in depth, demonstrated

Layered safeguards are easy to claim and hard to evidence. This one was
evidenced by accident, on document 05, and it is the single result this
project would most like read closely — because **every semantic layer passed
a record that was wrong, and the database caught it anyway, with no access to
the test oracle.**

The document prints its top rate bands as `Over $566,700`. The schema stores
brackets as inclusive integers, so that band begins at 566,701. Here is what
each layer did with it.

**1 — The conventions steered it wrong.** `CANONICAL_CONVENTIONS` enumerated
the open-*top* forms (`and over`, `or more`, `No limit`) and then said
*"transcribe, never re-derive"*. No rule covered an exclusive *lower* bound.
The specification, not the model, was the defect.

**2 — The model obeyed.** It transcribed `lower_bound: 566700`, exactly as
instructed, at confidence 0.94. Four records, one per filing status.

**3 — The independent verifier was unavailable.** On that document it
returned a body with no verdict envelope, three times. Containment did its
job — the records were flagged `verifier_unavailable` rather than blessed,
because silence is never assent — but the second opinion never arrived.

**4 — The adjudicator endorsed it at 0.95.** With `citations_valid: true`,
citing cell `p1_t0 r3,c4`, and concluding *"the persisted record matches the
page, so no change is needed"*. **The mechanical citation check passed too**,
and correctly: it verifies that the cited cells carry the figures the
resolution asserts, and `Over $566,700` genuinely contains 566700. The figure
was right. The derivation was wrong. No citation check can see that
difference.

**5 — The database refused all four.** An inclusive lower bound of 566,700
collides with the band below, whose upper bound *is* 566,700. The
`EXCLUDE USING gist` constraint rejected every one:

```
bracket_overlap: bracket [566700, and over] overlaps [64751, 566700] in the same chain
```

15 of 19 records persisted, 4 refused, **4 open review-queue rows** — the
no-silent-loss proof (anti-goal #8). The constraint needed to know only that
two intervals in one chain overlapped. It needed no ground truth, no model,
and no network.

**What it cost, and what it bought.** The harness later confirmed all four
against the oracle, so the constraint was right. But the constraint reached
that verdict *first*, and independently — which is the entire argument for
making bracket overlap unrepresentable in the DDL rather than validating it
in application code. Application-level validation would have been written by
the same author, from the same wrong understanding of `Over $X`, and would
have agreed with the mapper.

**And the near miss, which is the sharper half.** Those four records were
absent from the fact table, yet the adjudicator's 0.95 endorsement had
cleared threshold, citations, and mechanical support. It did not auto-close
them for one reason only: the flag happened to be `verifier_unavailable`,
which is default-denied for unrelated reasons. Had it been `confidence_floor`
— a rule this same document also produced — the item would have closed itself
as "verified-correct" over data the database had rejected, because
eligibility keyed on the queue row's *rule name* rather than on whether the
record was actually there. It now keys on presence, asked of the fact table
per item ([ADR 014 §8a-b](docs/decisions/014-semantic-layer-model-selection.md));
the two reachable paths are pinned by tests. Full evidence, including the
adjudicator's verbatim rationale, is in
[`docs/audit/evidence/`](docs/audit/evidence/).

---

## Model selection: a decision with a rule written before the numbers

The semantic layer runs `zai/glm-5.3-flash` for the mapper and adjudicator and
`alibaba/qwen-3-235b` for the verifier, both through the Vercel AI Gateway on
the Anthropic Messages protocol. That choice was made on cost — and **cost is
not evidence of fitness.** A cheap model that maps tax brackets wrongly is not
a saving.

The gap is wide enough to survive the vendor moving its price. Taking the
final gate's *actual* mapper token counts and repricing them three ways:

| Mapper, gate 6 token mix | Cost | vs Opus 5 |
|---|---|---|
| `zai/glm-5.3-flash`, price as measured 2026-08-26 | `$0.0203` | `96x` cheaper |
| `zai/glm-5.3-flash`, **promotion-day price** † | `$0.0405` | **`48x` cheaper** |
| `claude-opus-5` at list (`$5` / `$25`) | `$1.9503` | — |

The flash price doubled between the gate programme and promotion day (†, and
[§8l](docs/decisions/014-semantic-layer-model-selection.md)). The decision
does not turn on it: halving a `96x` advantage leaves `48x`, and the choice
would have been the same at either number.

So the rule that would overturn the choice was written **before the first
number existed** ([ADR 014 §3](docs/decisions/014-semantic-layer-model-selection.md)):
two triggers, explicit thresholds, and a named escalation target. Writing it
first is the entire point — a threshold chosen after seeing the data is a
rationalisation of whatever already happened.

### The finding of record

Six gate runs, every one measured against the same frozen specification:

> **Not one mapped value was ever wrong.** Across 1,250+ compared fields per
> scored run, every single difference the harness found was `actual <absent>` —
> a key the model did not emit. Never a misread figure, never a transposed
> digit, never a rate off by a factor of a hundred.

That is a sharper result than the score suggests, and it splits the problem in
two. The model's **reading** of these documents was correct and consistent from
the third run onward. What varied was its **adherence to a contract** — whether
each required key actually appeared — and adherence turns out to be the
stochastic part:

| Run | Score | What moved |
|---|---|---|
| 1-2 | 0/128 | transport framing, then identity vocabulary |
| 3 | 39/128 | bound semantics, unnamed attribute tail |
| **4** | **119/128** | one unreconciled paragraph — **the high-water mark** |
| 5 | 100/128 | the fix for that paragraph over-generalised |
| 6 | 81/128 | the revert of that fix deleted a load-bearing restatement |

Runs 5 and 6 were pre-registered repair attempts and **both made the number
worse**. They are reported at full weight, in order, beside the best result
rather than behind it. A specification tuned run-by-run against its own scoring
harness stops being a specification, and the shape of these six runs is the
evidence that this one was not.

### Why adherence was the variable, and why that is a transport fact

The gateway forwards the adapters' `output_config` json_schema request without
enforcing it for a non-Anthropic model. The contract is honoured by the model's
instruction-following, not by the transport. That was measured directly, not
assumed: a toy schema whose `required` list named a key the natural answer
omits came back missing it.

**So `required` in a JSON schema was a claim the transport did not check, and
the prompt was the only channel that actually carried the contract.** Four
separate failures traced to that one shape — the pipeline knowing something it
never told the model — before it was made unrepresentable by
`tests/mapping/test_prompt_schema_parity.py`, which walks every role's response
schema and asserts each required key is named in that role's prompt text.

This is the honest characterisation of running a cheap model on an unenforcing
transport, and it belongs next to the accuracy number rather than in a
footnote.

### The escalation: designed, wired, tested — and not funded

The pre-registered escalation was taken all the way to the edge and then
stopped, deliberately:

| Built | Where |
|---|---|
| Escalation rule, triggers, thresholds | [ADR 014 §3](docs/decisions/014-semantic-layer-model-selection.md), pre-registered |
| Venue analysis, both directions, trade named | ADR 014 §5, §8j |
| Per-role routing across two endpoints | `RECORD_VERIFIER_*` / `ADJUDICATOR_*` config chains, shipped |
| The inheritance trap that would have lost the run | [`tests/test_enforcement_arm_routing.py`](tests/test_enforcement_arm_routing.py) — 17 cases |
| Enforcement-probe methodology | [`scripts/probe_transport.py`](scripts/probe_transport.py) — asymmetric by construction |
| Price parity and cache ratios | live-confirmed from the model catalogue |

Everything except the invoice. **The spend was declined — an explicit,
cost-constrained engineering call, taken with the capability finished.**

The trap is worth one line on its own, because finding it is the argument for
building the thing you decline to run: routing the mapper to a second endpoint
while leaving the verifier's routing variables unset makes the verifier
*inherit* them — posting `alibaba/qwen-3-235b` to Anthropic's API with the
wrong key, failing every verifier call, and failing the gate. That was found by
reading the config chain before spending, not by losing a run to it.

**Trigger A never fired. The B-class triggers did, once.** Trigger A is the
semantic one, and at none of the six gates was a miss attributable to the
model's semantic judgment, because at none of them was a value wrong. The
conformance triggers are a different story and the record should carry it: at
the baseline run, **B1 fired at 3 hard contract failures and B2 at a 72.9%
malformed-item rate** ([ADR 014 §6](docs/decisions/014-semantic-layer-model-selection.md)).
Both were transport- and envelope-shaped, not reading-shaped. The escalation
they called for was, at that moment, *blocked rather than declined* — the §5
venue was direct Anthropic and that budget did not exist — so the
pre-registered hardening pass ran as the primary remedy and drove both to zero
(§7, §8). They stayed down: the closing gate recorded one hard failure that
retried and recovered, `item_ok` at 100% and B2 at 0%, which ADR 014 §8h reads
as not firing.

So the sentence that survives all six gates is narrower than "nothing fired",
and more useful: **a larger model was never shown to fix a contract-adherence
defect that a prompt change did fix, four times over.** Escalating would have
bought a different transport, not a better reading — a question worth answering
with a budget, and not one this exercise needed answered to be honest about
what it measured.

### What production runs

**The same configuration the gate measured.** No unmeasured configuration
ships: the same two models, the same endpoint, and the same frozen
`CANONICAL_CONVENTIONS`. The 81/128 record is a measurement *of the thing that
ships*, which is worth more than a better number measured on something else.

The freeze is now a command rather than a sentence — reproduce it:

```bash
uv run python -c "import hashlib; \
from tax_tables.adapters.anthropic_mapper import CANONICAL_CONVENTIONS as C; \
print(hashlib.sha256(C.encode()).hexdigest()[:16], len(C))"
# a5987cc0c324d1ac 18579
```

`tests/mapping/test_spec_freeze.py` asserts both values, so a reworded prompt
fails `make check`.

> **Deflating correction, 2026-08-27.** This paragraph previously read
> "hash-pinned at `sha256:88b9ca03eaafcf05`". **That literal reproduces under
> no recipe** — adversarial review hashed the constant at every commit in
> history under 11 normalisations, 3 encodings and 7 algorithms and found no
> match, and its derivation is lost. It was a number with no command behind
> it, in the one place the document claimed proof rather than assurance. The
> *invariance* it was standing for is real and independently checkable:
> `git diff fda868a..HEAD -- src/tax_tables/adapters/anthropic_mapper.py`
> is empty, so the text is byte-identical from the SPEC FREEZE v2 commit
> through HEAD, across every gate run in the dev-log.


## Cost

The headline finding is structural and holds on every target regardless of
what the runs measure:

**Four of five documents cost `$0.00` to extract, on all three targets.** The
router sends them to `pdfplumber`. Document 05 — the scanned one — is the only
document with nonzero extraction cost anywhere.

| Role | local | Vercel | AWS |
|---|---|---|---|
| Extraction, documents 01–04 | `$0.00` | `$0.00` | `$0.00` |
| Extraction, document 05 | Tesseract, `$0.00` (CPU) | vision-OCR, per-token | Textract, `$0.015`/page (list) |
| `SchemaMapper` | per-token | per-token | per-token |
| `RecordVerifier` | per-token | per-token | per-token |
| `Adjudicator` | per open queue item | per open queue item | per open queue item |

**Extraction fidelity is not equal across targets, and document 05 is where
they part.** This is a measured per-target limitation, stated because a
cost table that hid it would be selling the cheap column:

| Document 05 (scanned, no text layer) | Records | Measured |
|---|---|---|
| **local** — Tesseract | **19 / 19** | gate-measured, six runs |
| **Vercel** — vision-OCR (`zai/glm-5.3-flash`) | **3 / 19** | live-measured once, 2026-08-27 |
| **AWS** — Textract | — | never deployed; no measurement exists |

The Vercel figure is the vision path's **first end-to-end run**, and it
under-extracts: 3 `special_gain_rate` records arrived and the 16 bracket
records did not. It is **fail-visible, not silent** — the same run queued
**19 review items** naming what it could not place, which is anti-goal #8
working rather than a regression. No system binary can exist in a Vercel
function ([ADR 010](docs/decisions/010-vision-ocr-vercel-extractor.md)), so
this is the trade that target makes, and the honest reading is that **the
scanned path is production-ready on the local target and not yet on Vercel.**

The three per-token rows are the **Anthropic Messages protocol** billed at
whichever endpoint configuration selects. Measured here, live and local, that
is the **Vercel AI Gateway**: `zai/glm-5.3-flash` for the mapper and
adjudicator, `alibaba/qwen-3-235b` at `$0.22` / `$0.88` for the verifier.
Direct Anthropic and Bedrock are the other two config-selected routes and
would bill at their own list prices; neither is funded here, so no number in
this section came from either.

> **† Price footnote — flash moved mid-project.** Every measured cost line in
> this README was computed at `zai/glm-5.3-flash` = **`$0.075` / `$0.25`** per
> Mtok, the catalogue price read on 2026-08-26. On promotion day, 2026-08-27,
> the same catalogue read **`$0.15` / `$0.50`** (cache read `$0.03`, still
> `0.2x` input). `alibaba/qwen-3-235b` did not move. **Production is
> configured at the promotion-day price**; the historical tables below stay as
> they were measured, because a measurement is a record of what happened, not
> a running estimate. Because only the flash roles moved, a full run rescales
> by **`1.478x`**, not `2x`. Which of the two reads was wrong — or whether the
> vendor simply repriced — is arbitrated as far as the evidence allows in
> [ADR 014 §8l](docs/decisions/014-semantic-layer-model-selection.md), and the
> lesson is generalised in
> [Bottlenecks](#parallel-processing-and-bottlenecks): a cost line is a
> measurement with an expiry date.

Every price is configuration, not a hardcoded constant, and the accounting is
arithmetic rather than estimation: the extractor counts API calls, and the
model adapters read token usage off the response.

### Measured — the final run, itemized by document and role

Ten calls, **`$0.0424` for the whole corpus**. Every figure below is read off
the responses' token usage, not estimated.

| Document | Extraction | Mapper | Verifier | Total |
|---|---|---|---|---|
| `01_…_TY2026.pdf` | `$0.0000` pdfplumber | `$0.0033` | `$0.0043` | `$0.0076` |
| `02_…_TY2026.pdf` | `$0.0000` pdfplumber | `$0.0025` | `$0.0028` | `$0.0053` |
| `03_…_2026.pdf` | `$0.0000` pdfplumber | `$0.0078` | `$0.0077` | `$0.0155` |
| `04_…_2026.pdf` | `$0.0000` pdfplumber | `$0.0041` | `$0.0034` | `$0.0075` |
| `05_…_TY2025.pdf` | `$0.0000` Tesseract (CPU) | `$0.0026` | `$0.0039` | `$0.0065` |
| **Total** | **`$0.0000`** | **`$0.0203`** | **`$0.0221`** | **`$0.0424`** |

Three things this table is worth reading for:

- **The extraction column is zero all the way down.** Four documents have a
  text layer and the router keeps them off every paid engine; the fifth is
  scanned and Tesseract runs on CPU. The structural finding survives contact
  with the numbers.
- **Independent verification roughly doubles semantic-layer cost** — `$0.0221`
  against the mapper's `$0.0203` — and on this run it bought five true
  positives that named the exact missing attributes on document 05 before the
  oracle was consulted. That is the price of the second opinion, stated
  plainly so it can be judged.
- **`$0.0424` for 128 records across 5 documents** is `$0.00033` per record.
  The cost of the whole six-run gate programme was under `$0.30`.

The `Adjudicator` row above stays per-item rather than measured: it runs over
open review-queue items, and its cost is bounded by queue length rather than
by corpus size.

---

## Parallel processing and bottlenecks

The target: **10,000 documents/day.** Assume the realistic shape rather than a
flat average — 60% of volume inside a four-hour window:

```
6,000 documents / 4 h  =  1,500 documents/hour  =  0.42 documents/second
```

Required concurrency is `0.42 × T`, where `T` is the wall-clock seconds of one
document's pipeline. `T` is dominated by two model calls, not by extraction.

| `T` (seconds/document) | Concurrency needed at peak |
|---|---|
| 15 | ~7 |
| 30 | ~13 |
| 60 | ~25 |
| 120 | ~50 |
| **> 300 (measured on production, see below)** | **> 126** |

### `T` was measured on production, and it broke the deployment

**This is the sharpest result in this section, and it is a failure.** The
Phase 3.5-LIVE seed pushed all five fixtures at the production URL. Every
upload was accepted (`202`), every job was claimed within a second, and
**all five then exceeded the function's 300 s `maxDuration` and were killed
mid-pipeline.** Zero records persisted.

| Document | `T` measured locally, sequential | `T` on production, 5-way concurrent |
|---|---|---|
| 01 | 157 s | **> 300 s (killed)** |
| 02 | 173 s | **> 300 s (killed)** |
| 03 | 346 s | **> 300 s (killed)** |
| 04 | 268 s | **> 300 s (killed)** |
| 05 | 46 s (Tesseract) | **> 300 s (killed)** — and on vision-OCR, a heavier path |

Document 05 is not a like-for-like row: on Vercel it runs the vision-OCR
extractor rather than Tesseract, so its production `T` includes work the local
number never did. The other four are directly comparable, and three of them
roughly doubled.

**What that doubling is.** Not a slow function — a **rate limit, observed**.
Five documents concurrent means ten model calls contending for one free-tier
gateway allowance; the SDK absorbs `429`s by retrying with backoff, and
backoff is wall-clock that counts against `maxDuration`. This is bottleneck #1
below, reproducing exactly as predicted, at a fan-out of **five**. The
prediction was that provider rate limits would bind before anything else. They
did, three orders of magnitude below the design target.

Corroborating evidence that the work was real and simply ran out of clock: the
gateway billed **`$0.0222`** across the five killed runs — roughly a third of a
complete run's `$0.0626` — and then stopped exactly when the functions died.

**Two defects this exposed, stated plainly because neither is cosmetic:**

1. **`maxDuration` was undersized against its own written rule.** The
   project's own instruction is to size it to "the slowest single-document
   pipeline run plus margin". The slowest local run was **346 s**;
   `maxDuration` was set to **300 s**. That was already wrong before
   concurrency made it worse, and it was wrong for a reason worth naming: the
   number was chosen once, before the wall-clock column existed, and never
   re-derived when it did.

2. **A killed job is stranded, not retried.** `process_job` marks a job
   `running` when it claims it, but `sweep_pending` selected
   `WHERE status = 'queued'`. When the platform kills the invocation, nothing
   rewrites the row — so the cron backstop, which exists precisely to make a
   lost notification harmless, **could not see the job it most needed to see**.
   The five seeded jobs sat in `running` indefinitely, and because the SHA-256
   idempotency key treats a `running` job as live, re-uploading the same PDF
   returned the stranded job instead of starting a new one. The design note in
   `vercel_runner.py` — "a lost notification delays work, it never loses it" —
   holds for a *dropped kick* and did not hold for a *killed worker*.

**Neither was fixed while the gate was open.** Editing the runtime so a
measurement gate's own result reads better is the move this project has
refused at every prior gate. Both were fixed immediately after it closed, and
the repair is reported here with the same care as the failure.

#### The repair — a lease, and two numbers that were wrong

**A visibility timeout, which is what the AWS target gets for free.**
`sweep_pending` now claims a job that is `queued` **or** `running` past its
lease, so a worker the platform killed no longer takes its job down with it.
The schema already carried `attempt` and `started_at`; nothing migrated. Step
Functions gives the AWS adapter exactly this semantics as a platform
guarantee, and the Vercel adapter now buys it with one predicate — which is
the ports-and-adapters claim doing real work rather than decorating a diagram.

The invariant that makes it safe is written into the constant, not into a
comment somewhere else:

```
JOB_LEASE_SECONDS (default 1860)  MUST BE  >=  maxDuration (1800)
```

A *longer* lease only delays a rescue. A *shorter* one is the dangerous
direction: the sweep would reclaim a job whose worker is still alive, two
workers would map the same document, and the run would be billed twice.
`JOB_MAX_ATTEMPTS` (default 3) then abandons a job that reliably kills its
worker — recorded as `failed` with `error.type == "lease_expired_max_attempts"`
— because every reclaim spends model credit, and an unbounded retry loop over
a poisonous document is a way to convert a bug into an invoice.

**Two configuration numbers were corrected as conformance with the sizing rule
this project already wrote down, not as new policy:**

| Setting | Was | Now | Why |
|---|---|---|---|
| `maxDuration` | 300 s | **1800 s** | The slowest measured single-document run is 346 s, so 300 violated "slowest run plus margin" *before* concurrency made it worse. 1800 is the verified Pro ceiling (Vercel's limits table: Hobby 300, Pro 800 GA / 1800 extended). |
| cron sweep `limit` | 5 | **3** | `sweep_pending` processes its batch **sequentially in one invocation**, so `limit` and `maxDuration` are coupled. At `limit=5` the batch needs 96% of the budget with nothing left for the `429` backoff measured above. |

Both couplings are now **pinned by tests that read `vercel.json`**
(`TestLeaseInvariant` in `tests/api/test_jobs.py`), because a coupling nobody
checks is a coupling that drifts. The reclaim behaviour has its own four tests,
including `test_live_worker_is_never_stolen` — the one that would catch a lease
shortened below `maxDuration` by someone who had not read this section.

**What happened next.** The five stranded rows were closed first, by
`scripts/mark_stranded_jobs.py` — **mark, never delete: the rows are the
evidence** — because a `running` job reads as live to the SHA-256 key and
would have refused its own re-ingest. The repair was then promoted, and it
worked on the platform that had defeated it: the reclaim fired live at
`17:24:16Z`, taking a job whose worker the platform had killed and running it
to a terminal state — the exact transition that was impossible the day before.
A deliberately sequential re-seed followed and landed the **113** records the
live URL serves now. Nothing in this repair is pending: it is deployed,
exercised in production, and measured.

### Retries are safe, not free — the last measurement of the phase

The repairs above make a retry *correct*: a killed worker's job is reclaimed,
and re-ingesting replaces rather than duplicates, which is why the live record
count held at exactly `113` with zero duplicates through every reclaim. They do
not make a retry *cheap*.

**Measured, and it is the closing number of this phase: `~$0.09` of model spend
with nobody watching.** A five-document concurrent burst — my own operator
error during the re-seed, not a platform behaviour — left three jobs cycling
through their bounded retries. Each reclaim re-ran extraction, mapping and
verification from the top, and each one billed. The loop is bounded (`attempt`
is capped and the job is then abandoned as `failed`) and the data never moved,
so nothing was lost and nothing was wrong. It simply cost `~$0.09` to be
right, silently, over roughly an hour.

Three things follow, and they are the practical shape of retry economics at
10,000 documents/day:

1. **A retry ceiling is a spend ceiling.** `JOB_MAX_ATTEMPTS` is the only thing
   standing between a poison document and an unbounded bill; it is configuration
   for that reason, not a constant.
2. **Idempotent-and-expensive is a real category.** Correctness at the data
   layer says nothing about cost at the API layer, and the two need separate
   budgets. This system has both — the record replace, and the per-role token
   accounting — but only because the second was built for the accuracy harness
   and happened to fit.
3. **The cheapest retry is the one that never starts.** Reclaiming from the
   *last completed stage* rather than from the top would have made these three
   cycles nearly free. The pipeline does not checkpoint between extraction,
   mapping and verification, and on this corpus that is the single largest
   remaining efficiency left on the table.

### A cost line is a measurement with an expiry date

One more live finding, smaller but transferable. `zai/glm-5.3-flash` was
priced from the gateway catalogue at `$0.075` / `$0.25` per Mtok on
2026-08-26 and at `$0.15` / `$0.50` on 2026-08-27, one day later
([§8l](docs/decisions/014-semantic-layer-model-selection.md)). Nothing in this
repository was wrong; the vendor's number moved underneath it.

At 10,000 documents/day that is not a rounding error — it is the difference
between roughly `$41/day` and `$81/day` of mapper spend at the measured token
mix. Any capacity plan quoting per-token prices should therefore **record the
date it read them** and treat them as an input to re-read, not a constant. The
adapters already make this cheap: prices are configuration
(`*_USD_PER_MTOK_*`), so repricing is an environment change, not a code change.

### What breaks first, in order

**1 — Model provider rate limits. This is the real ceiling.** Two calls per
document. The per-call size is not a guess — it is the ten calls of the final
gate run, read off
[`docs/audit/evidence/2026-08-27-gate6-81-128.txt`](docs/audit/evidence/2026-08-27-gate6-81-128.txt):
**108,162 input tokens over 10 calls, a mean of 10,816 per call** (12,080 if
cache reads are counted, which most providers do). So at 0.42 documents/second
the pipeline asks for `0.42 × 2 × 10,816 ≈ 9,100 tokens/second`, or
**~545k tokens/minute** — ~609k on the cache-inclusive basis.

> **Corrected 2026-08-27.** This paragraph previously used "an order of 25k
> input tokens per call" and concluded ~1.26M TPM. The artifact says 10,816,
> so the estimate was high by roughly 2.3×. The *conclusion* is unchanged —
> 545k TPM still sits at or above the default organization tier on either
> provider, and rate limits are still what binds first — but a number
> reachable from a committed artifact should never have been an order-of
> guess, least of all in the section whose whole claim is that it is
> quantitative.

Mitigations, in
the order they should be reached for: (a) the router already keeps 4 of 5
documents off the model path for *extraction*, so only the semantic layer
scales with volume; (b) `RECORD_VERIFIER_MODEL` deliberately accepts a
different, cheaper model than the mapper — the same config knob that mitigates
correlated same-model errors also halves the expensive-model TPM; (c) batching
adjudication, which is already per-item and independent; (d) a provisioned
throughput commitment on Bedrock. **A rate limit must never become data loss:**
a throttled document fails its job row with a reason and stays re-runnable.

**2 — Fan-out concurrency, and the fact that it is not one number.**
`MAX_CONCURRENT_DOCUMENTS = 8` bounds *one Map Run*, not the account. The
audit's sharpest finding here was that with nothing reserved, a large batch and
the public read path draw from the same Lambda concurrency pool — so a big
ingest makes `GET /records` return 429s, and a burst of reads stalls the
pipeline. Both directions are now fixed by reserved concurrency (8 per pipeline
step, 25 for the API). Raising throughput means raising `MAX_CONCURRENT_DOCUMENTS`
*and* the matching reservations *and* checking (1) first — the fan-out knob is
worthless if the model provider is already the constraint.

**3 — Connection exhaustion under fan-out.** This is the reason RDS Proxy
(AWS) and the pooled Neon endpoint (Vercel) are in the design at all. Postgres
connections are a fixed, small resource; N concurrent documents × the steps
that touch the database is a multiplier that will exhaust `max_connections`
long before CPU. The proxy's pool is now explicitly sized
(`max_connections_percent=90`, `max_idle_connections_percent=50`,
`borrow_timeout=30s`) — it had synthesized *empty*, which meant the stack's
stated mitigation was a claim rather than a setting. Application-side, every
connection is opened and closed per request or per job, which is the
request-scoped model Vercel enforces anyway.

**4 — Step Functions payload quota.** The Distributed Map passes each
document's extracted grid and mapped records between states, against a **256 KB**
per-state payload limit. Document 03 (51 records across 2 pages) is the one
that gets close. The Map Run's aggregate output is already offloaded to S3 via
`ResultWriter`; the **inter-step** payload is not, and offloading it to S3 is a
named open design item, not a solved one. See
[Honest limitations](#honest-limitations).

**5 — Ingest volume.** 10,000 documents/day at up to 10 MB each is up to
100 GB/day of PDFs. This is exactly the threshold at which the
blob-in-Postgres adapter stops being correct and S3 (or Vercel Blob) becomes
the right store — the trade-off is documented in
[ADR 011](docs/decisions/011-blob-in-postgres-vs-vercel-blob.md), including
the volume at which to switch.

**Not bottlenecks, and why:** API Gateway's default 10,000 RPS is three orders
of magnitude above 0.42/s; Standard Workflow state transitions (4,000/s
account-wide) are likewise not binding at 4 states per document; and
extraction is CPU-bound at zero marginal cost for 4 of 5 documents.

### Where batch-level failure is visible

The Distributed Map runs with `tolerated_failure_percentage=100`, and AWS is
precise about what that means: *"If you specify the percentage as 100, the
workflow won't fail even if all child workflow executions fail."* That is
chosen, not conceded — the Map is **transport**, documents are independent, and
one malformed PDF in a batch of 500 must not abort the 499 that are fine.

The setting is therefore never standalone. Because the Map Run's own status is
uninformative by construction, failure is made legible at both grains:

- **per document — the `jobs` table is the source of truth.** Every pipeline
  step catches into a `MarkFailed` step that writes `status='failed'` with the
  reason, and `GET /jobs/{id}` serves it. Without that catch, this setting
  would convert a loud batch abort into a job stuck at `running` forever:
  silent loss, the worst failure mode this product defines. Every handler binds
  `job_id` and `document_id` as Powertools `Logger` keys, so all five steps'
  log lines grep back to one document — which is the question a fan-out
  actually poses.
- **per batch — a metric and an alarm.** The Map Run is *labelled*, so its
  child executions emit `AWS/States ExecutionsFailed` under
  `<state-machine-arn>/PerDocument` — one datapoint per failed document, while
  the parent execution stays green. `DocumentFailures` alarms on it;
  `PipelineFailures` covers the disjoint case where transport itself broke.
  Both notify an encrypted SNS topic.
- **per run — a report.** `ResultWriter` exports every child execution's
  outcome to its own S3 bucket, and the Map Run's item counts
  (Failed / Aborted / Pending / Succeeded) are available from `DescribeMapRun`.

So: the Map never fails, and every failed document does — once in the database
the API serves, once in a metric an operator can be paged on.

---

## Fixture design

**The five PDFs and `fixtures/ground_truth.json` are self-authored.** The
brief supplied no documents. Rather than treat that as a gap, the fixtures are
the test engineering: each document breaks a different naive assumption, and
the ground truth records the traps explicitly.

> All values are **synthetic**. These are not real tax tables and must not be
> used as such.

| File | Shape | What it breaks |
|---|---|---|
| `01_..._TY2026.pdf` | Wide matrix, 7 rate rows × 4 filing-status columns, two-level header with a merged cell | One *visual* row is four *logical* records. Top bracket open-ended (`and over`) |
| `02_..._TY2026.pdf` | Small tables plus prose | Amounts carry no `$`. One rule exists only in a prose sentence. Carries a prior-year column |
| `03_..._2026.pdf` | 51 rows across 2 pages | Repeated header and `(continued)`. Rates carry no `%` — the unit is stated only in body text. A long dash means *no tax imposed* (NULL, not zero). One legitimately negative rate. One derived column |
| `04_..._2026.pdf` | Landscape, four separate tables on one page | Mixed units in one document. `No limit` as a value. Records that are not brackets at all |
| `05_..._TY2025.pdf` | **Scanned image, no text layer** | `pdfplumber` returns nothing. A different range separator (`to`). One rate exists only in a footnote. The document is **superseded** and must not surface in `tax_year=2026` queries |

Two traps are worth stating on their own, because they are the ones that
quietly corrupt a dataset rather than crash a parser:

- **Document 02 carries the ID `TB-2025-14` but applies to tax year 2026.** Tax
  bulletins are issued in November of the preceding year. `tax_year` must be
  read from the effective-date sentence in the body — never inferred from the
  filename or the document ID.
- **Documents 02 and 04 each hold two tax years in one row** (a current and a
  prior column). An extractor that keeps one column silently loses half the
  records, and reports success.

`fixtures/ground_truth.json` is the **test oracle**. No module under `src/`
may read, import, or embed a value from it; the accuracy harness under
`tests/accuracy/` is the only consumer. This is verifiable with `grep`, and
the packaging layer reinforces it — the ground truth is excluded from any
deployed bundle.

---

## The three deployment targets

### 1. AWS — designed in full, **never deployed**

There is no AWS account and no budget. The design is expressed as a Python
CDK v2 stack that proves itself **offline**, on every push:

- `cdk synth` with **no AWS credentials present** (`cdk-nag`'s
  `AwsSolutionsChecks` runs as an aspect, so one unsuppressed error fails the
  synth);
- `cfn-lint` over the synthesized template;
- the synthesized cloud assembly committed under `infra/cdk.out/` as the
  reviewable artifact.

Current gate: synth exit 0 credential-stripped, `cfn-lint` clean, cdk-nag
**58 compliant / 53 suppressed / 0 non-compliant**, every suppression carrying
an individually written justification next to the resource it covers.

This stack has never met a real control plane. Statements about its
deploy-time behaviour come from documentation and adversarial audit, not from
a deployment — and the audit was worth it: synth-plus-lint-plus-nag green was
nowhere near deploy-correct. See [`docs/audit/`](docs/audit/) for the full
75-finding ledger.

### 2. Vercel — the live URL

> **Platform behaviour worth knowing before you deploy:** Vercel assigns a
> project's **first** deployment to **production**, whatever target you asked
> for — `vercel deploy` with no `--prod` still lands in production and takes
> the production alias. Pass `--target=preview` explicitly on an empty
> project. (Hit and recorded during Phase 3.5; see the dev-log.)

> **Live in production since 2026-08-27:**
> **https://auxis-johan-rocuts-projects.vercel.app**
> — public, no SSO gate, every `GET` serving, **113 of 128 records
> persisted with zero duplicates.**
>
> | Production, measured 2026-08-27 (current at this commit) | Result |
> |---|---|
> | `GET` endpoints (`/records`, `/documents`, `/reviews`, filtered) | **200**, correct shapes |
> | **Records persisted** | **113 / 128**, **0 duplicates**, all `active` |
> | — by document | `01` 32/32 · `02` 8/8 · `03` 51/51 · `04` 19/18 · `05` **3/19** |
> | Open review-queue items | **38** — every value the pipeline would not guess |
> | Warm latency, data path, median of 3 | **272 ms** (`/documents` 278 ms, `/records/resolve` 283 ms) |
> | True first click, cold function **+ Neon resume** | **6.76 s** — *measured on preview*, see below |
> | `POST /documents`, no key / wrong key | **401 / 401** |
> | `POST /internal/sweep`, no bearer / wrong bearer / cron bearer | **401 / 401 / 200** |
> | **Total project spend, all six gate runs and every live seed** | **`$0.74`** of a `$5.00` free allowance |
>
> **Copy-paste the headline query.** It proves the range index and the
> bracket-integrity constraint deliver something real, over the live URL:
>
> ```bash
> curl -s "https://auxis-johan-rocuts-projects.vercel.app/records/resolve\
> ?amount=150000&filing_status=single&taxpayer_class=individual&tax_year=2026"
> # -> the 106151-202650 bracket at 0.24, active, with its page and table
> ```
>
> Two honest caveats on that table. **Document 05 is 3 of 19** — the scanned
> one, on Vercel's vision path, against 19/19 on the local Tesseract path; it
> is a measured [per-target limitation](#cost), fail-visible via 19 of those
> 38 review items. And **document 04 is 19 against an expected 18**: a sixth
> `surtax_threshold` with a null `filing_status`, which the model declined to
> emit at the gates and emitted here — the same stochasticity ADR 014
> measured, admitted legitimately by the natural key rather than smuggled past
> it. Both are dissected in [`docs/dev-log.md`](docs/dev-log.md).
>
> ### What deploying for real taught the system
>
> Everything above works because three defects were found **on the live URL
> and nowhere else** — not by the 705-test suite, not by the six gate runs,
> not by the adversarial audit. Each was fixed test-first, after the gate that
> found it had closed:
>
> | Defect | How it showed up | Fix |
> |---|---|---|
> | **A killed worker stranded its job forever** | The platform killed a worker at `maxDuration`; nothing rewrote the row, and the cron sweep claimed only `queued`, so it was blind to exactly the failure it exists to cover. Five jobs sat in `running` permanently. | Lease / visibility timeout, the semantics Step Functions gives the AWS target for free |
> | **A retry duplicated records** | 60 rows for a 32-record document. The natural-key upsert assumes a *deterministic* producer; [ADR 014 §8](docs/decisions/014-semantic-layer-model-selection.md) measured that ours is not, and two runs wrote two valid keys for one bracket. | Re-ingest became a document-scoped atomic **replace** — idempotence moved to the level that owns the invariant |
> | **Adjudication outran its own budget** | The wall-clock check sat *between* items while one call could spend `300 s × 4` SDK attempts. One document overran a 1800 s invocation three times with its records already correct. | Per-item ceiling bounded to `90 s × 2`, so the budget actually binds |
>
> None of these are exotic. They are the ordinary failure modes of
> request-scoped compute meeting a nondeterministic producer, and the useful
> observation is that **a green test suite and a passing accuracy gate proved
> neither of them.** The second one is the most interesting: it is a *data
> integrity* consequence of a *model behaviour* finding, and the two were
> measured six weeks and three phases apart.
>
> The repairs landed **after** each gate closed, never during — a measurement
> gate whose subject is edited mid-measurement is not a measurement.
>
> **The cold chain is no longer observable on production, by construction.**
> `vercel.json` registers a one-minute cron on `/internal/sweep`; that request
> touches the jobs table every 60 s, so Neon never reaches its 5-minute
> autosuspend and the function never goes cold. An evaluator's first click is
> therefore always a warm click — good for a demo URL, and the reason the
> cold-chain number above comes from the **preview**, which has no cron.
> Preview and production share one Neon database, so the resume cost measured
> there is the same resume cost production would pay if its cron were removed.
>
> Measured against the preview, where the cliff is observable:
>
> | Measurement | Result |
> |---|---|
> | **True first click** — cold function **+ Neon resume** from autosuspend | **6.76 s** (server 4.42 s; DNS 2.18 s) |
> | Cold function, database already active | **0.67 s** |
> | Warm requests | **0.37–0.43 s** (median ~0.39 s) |
> | Request-body cap | **~4.5 MB** (4,482,662 B accepted / 4,495,769 B rejected) |
>
> The first two rows are **different measurements and only the first is what an
> evaluator's first click costs.** The 0.67 s figure was taken minutes after a
> seed run, so the function was cold but Neon was still active. The 6.76 s
> figure comes from leaving the deployment completely untouched for 430 s —
> past Neon's 5-minute autosuspend — and then issuing exactly one request to a
> data-path endpoint. Splitting it by `curl`'s timings: 2.18 s client-side DNS
> for a brand-new hostname (variable, and near-zero on the earlier
> fresh-hostname run), 0.16 s TCP+TLS, and **4.42 s server-side** — of which
> roughly 0.5 s is the function and the remaining **~3.9 s is Neon waking up**.
>
> **A prediction this measurement did not confirm.**
> [ADR 004](docs/decisions/004-aurora-serverless-v2-rejected.md) rejected
> Aurora Serverless v2 over its documented ~15 s resume, and the expectation
> going in was that the chosen stack would win by an order of magnitude. It
> does not. First click against first click it is **6.76 s vs ~15 s — about
> 2.2×**; comparing resume to resume it is **~3.9 s vs ~15 s, about 3.8×**.
> Still a decisive win, and it widens to roughly 4.5× against Serverless v2's
> documented 30 s+ after a day of idleness — but "an order of magnitude" was
> the guess and ~2–4× is the measurement. The ADR's conclusion survives; its
> margin was overstated, and both numbers are now measured rather than one
> being argued.
>
> The honest reading is that **this stack has an autosuspend cliff too** — it
> is simply a much smaller one. A first click after idleness costs seconds, not
> milliseconds, on any scale-to-zero database.
>
> Endpoint sweep against the preview: every `GET` returns correctly, unknown
> ids 404, `/records/resolve` 422s without a chain, unauthenticated and
> wrong-key `POST /documents` both 401, and `/internal/sweep` rejects a
> missing or wrong bearer on **both** `GET` and `POST` while accepting the
> cron bearer. All five fixtures upload **202**; each job then reaches the
> honest `missing_credentials` terminal state, which is the credential block
> showing up exactly where it should — in the job row, not as an HTTP error
> on a valid upload.
>
> Two preview-specific notes. **Vercel crons run on production only**, so on a
> preview the sweep is exercised by direct authenticated call. And of the five
> uploads, four were carried to terminal state by the self-kick while one
> stayed `queued` — the kick is best-effort by design (the daemon thread need
> not outlive the response on request-scoped compute) and a direct sweep
> drained it immediately. That is the cron-backstop contract working, observed
> rather than assumed.
>
> The decisions behind these adapters are recorded in ADRs
> [008](docs/decisions/008-vercel-as-the-live-target.md),
> [009](docs/decisions/009-cron-sweep-jobrunner.md),
> [010](docs/decisions/010-vision-ocr-vercel-extractor.md), and
> [011](docs/decisions/011-blob-in-postgres-vs-vercel-blob.md).

#### Reproducing the seed against a deployment

`scripts/seed_remote.sh` POSTs the five fixtures to a deployed URL with the
API key and then polls each job to a terminal state. Its exit status is the
point: non-zero if any upload was rejected or any job ended `failed`, and a
job still `queued`/`running` when the budget runs out is reported as a
**timeout**, never quietly counted as a success.

```bash
BASE_URL=https://<deployment> API_KEY=... POLL_SECONDS=1800 scripts/seed_remote.sh
```

`POLL_SECONDS` defaults to 180, which is **below the slowest measured
single-document run (346 s)** — a leftover from before that column existed.
Set it from the deployment you are seeding rather than accepting the default,
or a slow run reports as a timeout. The default is documented rather than
silently raised, for the same reason the stranded rows are marked rather than
deleted: the number is evidence of when it was chosen.

### 3. docker-compose — the evaluator's local reproduction

**Postgres only.** `docker compose up -d --wait db` starts a `postgres:18`
container — the same major as the Neon branch — and that is the whole compose
file: no API image, no worker service, no Dockerfile. The application runs on
the host under `uv`, which is what the test suite does too, so the local path
is exercised on every `make check` rather than only when someone tries it.

`uv run python -m tax_tables.migrate` applies all eight migrations; `make api`
serves the full surface; a document is ingested with the two calls in
[Quick start](#quick-start). It works from a fresh clone, and the heading says
"local reproduction" rather than "one command" because it is four.

---

## Honest limitations

Consolidated, and deliberately specific. If something is unproven, it says so.

### Known and accepted, with the fix shape written down

Three defects an external adversarial review confirmed on 2026-08-27 that are
**not fixed here**. Each is stated with what it costs and what closing it
would take, because "we did not get to it" and "we did not notice" are
different sentences and only the first is true.

**A Postgres backend sits `idle in transaction` for the whole of a job.**
`service/jobs.py` opens a connection, claims the job, then reads the filename
outside any explicit `transaction()` on a non-autocommit connection — so an
implicit transaction stays open across the model calls, measured at 331 s for
document 03's mapper alone. A transaction-mode pooler cannot multiplex that
slot, which is exactly the mitigation bottleneck #3 leans on, and it pins the
`xmin` horizon while it is held. At the demo's sequential five-document scale
the practical impact is nil; at the 10,000/day target it is the connection
ceiling arriving early. **Fix shape:** release the claim transaction before
the model calls and re-claim by id to persist. **Why not now:** it moves
transaction boundaries around the pipeline's only write path, days after
three other concurrency fixes; the risk asymmetry this close to delivery
favours the documented defect over the rushed repair. This is bottleneck #3
confirmed live rather than predicted.

**Record `confidence` is uncalibrated.** 91 of the 113 live records sit at
exactly `1.000`, and the extraction floor is a literal `1.0` on both live
paths, so `min(self_report, floor)` reduces to the model's self-report. The
`confidence_floor` triage rule has produced **zero** of the queue items. The
field is therefore a *self-report*, not a calibrated probability, and
`min_confidence` filters on it accordingly. The README elsewhere reports
high-confidence-wrong records at length; this says the underlying field was
never calibrated to begin with. **Fix shape:** calibrate against the oracle
and report a reliability curve — real work, not an afternoon.

**ADR 014 is 1,279 lines against the ADR index's own "one page each".** That
is deliberate and it is the one convention this project knowingly breaks: 014
is not a decision record, it is the *evidence* record for model selection —
six gate runs, two falsified predictions, a pre-registered escalation rule and
its unfunded close. Splitting it would scatter the only through-line that
makes the 81/128 number legible. The index is annotated to say so, so a
reader meets the exception before the exception meets them.

### Gates still open

1. **The accuracy gate closes at 81/128, every failing record is named, and
   the best run scored 119/128.** Six runs ship as evidence, in order: 0/128
   baseline (transport), 0/128 hardened (identity vocabulary), 39/128 (bound
   semantics and an unnamed attribute tail), **119/128** (one unreconciled
   paragraph), 100/128 (the fix for that paragraph over-generalised), 81/128
   (the revert of that fix deleted a restatement that was load-bearing). The
   last two runs were repair attempts, both pre-registered, and both made the
   number worse — that is the honest shape of the result and it is not
   smoothed over. **No mapped value was wrong in any run**: all 26 differing
   fields in the final run are `actual <absent>`, a key not emitted. The
   47 failures are two groups — 28 on `ordinary_income_bracket.taxpayer_class`
   (document 01) and 19 on document 05, all missing `superseded_effective`.
   See [Accuracy](#accuracy) for the naming, the cause, and the measured blast
   radius of the final revert.

   It ships at 81/128 rather than being tuned further, and the higher earlier
   score is recorded beside it rather than quietly replacing it — a spec tuned
   run-by-run against a scoring harness stops being a specification. **The
   cause of the 28 remains unidentified**: the sixth run falsified both the
   leading hypothesis and the competing one, which is a real open question and
   is stated as one. **The escalation to a larger model was designed, wired,
   tested — and deliberately not funded.** The *semantic* trigger never fired
   at any of the six gates, because at no gate was a miss attributable to the
   model's semantic judgment; the conformance triggers fired once, at the
   baseline, and the hardening pass — not a larger model — cleared them. The
   failures were contract adherence throughout, and a larger model was never
   shown to fix them. See
   [Model selection](#model-selection-a-decision-with-a-rule-written-before-the-numbers)
   for the full close. Full tables in [`docs/dev-log.md`](docs/dev-log.md) and
   [ADR 014 §6-8k](docs/decisions/014-semantic-layer-model-selection.md).
2. **Four identity fields are encodings, not extractions — and that boundary
   is deliberate.** `jurisdiction` is `US-FED` or `US-<ISO 3166-2 code>`,
   `taxpayer_class` is `individual` or `estate_or_trust`, and `attribute_key`
   draws on a fixed vocabulary. None of those strings is printed in any
   fixture. They are **target-schema encodings**, adopted the same way the
   `RecordType` and `FilingStatus` enums already were, and the test that
   separates them from cheating is extractability: `US-FED` appears in no PDF,
   so it cannot be extracted, only agreed. A per-record *value* — Alabama's
   printed `4.000` — is the opposite case and is never taken from anywhere but
   the page. `src/` never opens `fixtures/ground_truth.json` at runtime; see
   [ADR 014 §8](docs/decisions/014-semantic-layer-model-selection.md).
3. **Conformance measures the contract, never the truth — the harness is the
   truth check.** Document 01 once produced a run with *zero* contract
   failures and two real semantic defects: the verifier disputed a record on a
   false premise (claiming it held `257300` when it held `257250`), and the
   adjudicator auto-resolved that dispute at 0.98 confidence while repeating
   the false premise, having been given the record's true value. Every
   response involved was perfectly schema-conformant, so the conformance table
   showed nothing. Two gates now stand in front of an unattended close —
   dispute-born queue items can never auto-resolve, and any auto-resolution
   must be mechanically supported by the figures in its own cited cells — but
   the general point stands and is worth stating plainly: **a clean
   conformance table is not evidence of correct data.**
4. **Some discriminators are asserted from convention, not read from the
   page.** Document 01 names no jurisdiction anywhere in its text, so
   `jurisdiction: "US"` (and `currency: "USD"` where no sign appears) comes
   from the canonical conventions. Such fields are now declared in a
   `convention_derived` list on the record rather than given a provenance
   citation they cannot support — see
   [ADR 015](docs/decisions/015-convention-derived-discriminators.md).
5. **The vision-OCR adapter has exactly one live end-to-end run, and it
   under-extracts.** It is built, and its 26 tests cover every fidelity and
   fail-closed rule against recorded response shapes. `zai/glm-5.3-flash` is
   vision-capable on this credential: a single authorized probe transcribed a
   rendered `RATE 15.3%` / `OVER $250,000` fragment exactly, for 52 input
   tokens
   ([ADR 010 addendum](docs/decisions/010-vision-ocr-vercel-extractor.md)).

   **The re-seed then measured the branch on production — once.** Document 05
   yielded **3 of 19** records on the vision path, and the same run queued
   **19 review items** naming what it could not place. Which of the two
   pre-registered outcomes the branch takes is therefore no longer an open
   question: it is **fail-visible, not silent**, the fallback working rather
   than a regression, and the number is in [Cost](#cost).

   What remains genuinely unasserted is **fidelity at full quality**: no run
   on this target has read document 05's merged header cells, its `to` range
   separator and its footnote-only rate correctly. One under-extracting run
   measures the failure mode, not the ceiling. Document 05 is extracted
   completely today by Tesseract in `docker compose`, or by Textract in the
   AWS design.

6. **The production pipeline carries documents end to end, and one of the
   three questions this item used to list is still open.** The first 3.5-LIVE
   seed put all five fixtures into a `maxDuration` kill and persisted zero
   records; the lease/visibility timeout, the re-derived 1800 s `maxDuration`
   and the `limit=3` cron were then deployed, and two of the three unknowns
   closed on the live URL: **a reclaimed job does complete on the platform
   that killed it** (observed `17:24:16Z`), and **document 05's vision branch
   under-extracts at 3 of 19** rather than failing closed (item 5). What
   remains genuinely unproven is the third: **1800 s has never been shown to
   be enough clock for a five-way concurrent fan-out** — the successful
   re-seed was deliberately sequential, and the one concurrent burst that did
   happen was an operator error whose documents landed correctly but whose
   timings are not gate-comparable. Closing it would take one deliberate
   concurrent seed against the live URL, which was not run here; the live
   dataset ships as delivered.

### The AWS stack

7. **It synthesizes and validates but was never deployed.** No template here
   has met a real control plane.
8. **The Lambda deploy artifact is incomplete by design.** The functions ship
   the real `src/` tree and the handlers are real, unit-tested code — but the
   runtime dependency layer (psycopg, pydantic, anthropic, boto3, mangum,
   aws-lambda-powertools) is a
   deploy-pipeline build step that intentionally does not exist. Likewise the
   `app_ingest` database role the Lambdas IAM-auth into is created by a
   deploy-time migration, not by the stack, and `API_KEY` / `CRON_SECRET` are
   deploy-time provisioning.
9. **Step Functions inter-step payloads are not offloaded.** Each document's
   extracted grid and mapped records ride the 256 KB per-state payload quota.
   The Map Run's *aggregate* output is exported to S3; the inter-step payload
   is not. Document 03 is the one that approaches the limit.
10. **The documented 10 MB upload cap is a per-target number, and only one
   target's real ceiling is known.** The application-level cap
   (`MAX_UPLOAD_BYTES`, default 10 MB) is enforced before a byte reaches the
   pipeline, but each platform imposes its own body limit underneath it, and
   the smaller of the two wins.

   | Target | Real ceiling | Basis |
   |---|---|---|
   | local / `docker compose` | 10 MB | the application cap; nothing smaller underneath |
   | AWS | **~4.4 MB** | API Gateway caps payloads at 10 MB, but a Lambda proxy integration base64-encodes the binary body into a 6 MB synchronous invocation payload — 6 MB ÷ 4/3. Derived, not measured. |
   | Vercel | **~4.5 MB — measured** | Bisected against a live preview: **4,482,662 bytes accepted (202), 4,495,769 bytes rejected (413)**. The rejection is the platform's `FUNCTION_PAYLOAD_TOO_LARGE`, raised at the edge — the request never reaches the function, so the app's own 413 and its JSON `detail` never fire and the client gets an HTML error instead. Note this contradicts the current platform documentation, which states 100 MB request bodies; the historical 4.5 MB is what this project's plan and runtime actually enforce. Quoting the doc would have been wrong. |

   The application cap (`MAX_UPLOAD_BYTES`, 10 MB) is therefore only binding
   on the local target; on both deployed targets a *smaller* platform limit
   fires first, and on neither does the caller receive the app's own message.
   Uploads above a platform ceiling would need a presigned-upload ingest
   path, which is designed-for but not built.
11. **The VPC endpoint policies are account-scoped, not action-scoped.** All
   seven endpoints require `aws:PrincipalAccount` to be this account, which
   closes the cross-account exfiltration path an unrestricted S3 gateway
   endpoint otherwise opens. They deliberately do not enumerate actions: an
   over-tight endpoint policy is a deploy-time failure this project cannot
   test.
12. **Two runtime AWS calls have no endpoint, deliberately.** The VPC has no
   NAT and no internet path, so every runtime AWS API call must traverse a VPC
   endpoint. All 33 enumerated calls resolve to one of the seven endpoints (S3
   gateway; interface: Secrets Manager, Textract, Bedrock runtime, CloudWatch
   Logs, Step Functions, X-Ray) or to a path that never leaves the local host
   (`rds-db:connect` token signing is local SigV4; Lambda credentials arrive
   via the runtime). Two gaps remain, on purpose:

   | Call | Status | Why it is acceptable — and when it stops being |
   |---|---|---|
   | `bedrock:GetInferenceProfile` / profile-routed invocation | no endpoint, not granted | The stack pins foundation-model IDs. Adopting cross-region inference profiles would require both the IAM grant on the profile ARN and routing this VPC cannot express today. |
   | `cloudwatch:PutMetricData` | no endpoint | No code emits custom metrics. Enabling them requires the `monitoring` interface endpoint first. Note this does **not** affect the failure alarms above: those read `AWS/States` metrics the service publishes itself. |

13. **One accepted IAM over-grant, stated rather than hidden.** The Lambda
   execution roles carry `AWSLambdaBasicExecutionRole` and
   `AWSLambdaVPCAccessExecutionRole`, both of which grant on `Resource "*"`.
   The ENI half genuinely cannot be narrowed (the `ec2:Describe*` calls
   support no resource-level permissions). The **logs half could be** — every
   function here has an explicit log group — and narrowing it means replacing
   CDK's default role on six functions. That is recorded as an open hardening
   item, not claimed as done. Separately, the API Gateway account role's
   `AmazonAPIGatewayPushToCloudWatchLogs` grants `logs:GetLogEvents` and
   `logs:FilterLogEvents` on `*` — account-wide log **read**, materially more
   than its name suggests. It is accepted because the role is assumable only
   by `apigateway.amazonaws.com` and it is the policy AWS requires for API
   Gateway CloudWatch logging.

### The service

14. **The review queue is readable over HTTP but not writable, on purpose.**
    `GET /reviews` and `GET /reviews/{id}` expose every queued item with its
    provenance and its full adjudication audit trail — including the
    below-threshold *proposals* the adjudicator stores on items that stay
    open, since a reviewer cannot act on a proposal they cannot see. There is
    **no write path**: resolving or dismissing an item is a human judgment
    with legal weight over tax data, and exposing it would mean designing
    reviewer identity, authorization, and an approval trail — none of which
    this exercise scopes. A human resolves the remainder through the
    database, where the `closed_rows_carry_audit_trail` constraint makes a
    closed item without its `resolved_by` / `resolved_at` unrepresentable
    whichever route closed it. The omission is asserted by a contract test,
    not merely intended.
15. **`GET` endpoints are unauthenticated by design**, being read-only tax
    data. The write path enforces `X-API-Key` with a constant-time compare,
    and per-IP rate limiting is an edge rule (WAF on AWS; a Vercel Firewall
    rule when Phase 3.5 lands), not application code.
16. **The Textract fixture is hand-constructed.** No AWS credentials ever
    existed, so `fixtures/textract/05_response.json` was built from the
    documented `BLOCK` / `CELL` / `RELATIONSHIPS` shape and is labelled as
    such in the JSON itself, in its generator, and in the tests. Its content
    was transcribed from the real scanned fixture via the local OCR; the
    oracle was never opened. Deviations are recorded in the generator
    docstring.
17. **Bedrock structured-output acceptance is unverified.** The Bedrock
    adapters are real and fixture-tested, but whether a live Bedrock runtime
    accepts the exact structured-output request shape is a deploy-time
    verification item. The parsers fail closed, so the failure would be loud.
18. **The mechanical citation check validates figures, not derivations —
    and cannot be made to.** Before an adjudication may auto-close a review
    item, `resolution_is_supported` requires every number the resolution
    asserts to appear in a cited cell, or to be reachable from one by a
    documented transform. That catches an invented figure. It is structurally
    blind to the *right figure reached by the wrong reasoning*: a resolution
    asserting `lower_bound 566700` while citing a cell reading
    `Over $566,700` passes, because the figure is genuinely there. Document
    05 produced exactly that, at 0.95 confidence, on all four of its top
    bands (see [Defense in depth](#defense-in-depth-demonstrated)).

    This is a real limit and the design answers it by placement rather than
    by patching the check. Convention errors of that class are caught one
    layer down, by the bracket-integrity constraint, which compares a
    record against its *neighbours* instead of against its own citation —
    and that is why bracket overlap is a database constraint here and not an
    application-level validator. A citation check reasons about one record
    in isolation and can never see a collision; making it try would be
    scope it cannot carry. The honest statement is that no single layer
    catches everything, which is the reason there is more than one.

---

## Development log, decisions, and how AI was used

- [`docs/dev-log.md`](docs/dev-log.md) — what was tried, what failed, what was
  chosen and rejected, in order.
- [`docs/decisions/`](docs/decisions/) — one ADR per real decision, including
  the rejections (Aurora DSQL, the RDS Data API, Aurora Serverless v2,
  Terraform/CDKTF) with the threshold at which each rejection would flip.
- [`docs/audit/`](docs/audit/) — the complete 75-finding ledger from the Phase 4
  adversarial audit, with every finding's disposition, **including the 23 that
  remain named-but-unverified by design**.

### How AI assistance was used

This is an AI Engineer exercise, so the honest account is signal rather than
something to bury.

The service *itself* uses models in exactly three places, all bounded and all
documented above: the mapper, the independent verifier, and the queue
adjudicator. Extraction, routing, validation, and persistence are
deterministic code, on purpose.

For *building* it, AI assistance was used throughout — for implementation,
and more usefully as an adversarial reviewer. The Phase 4 audit was 23 agents
run over the committed tree: six resource-type auditors, three directed lenses
(suppression justifications, currency of hardcoded facts, isolated-VPC
completeness), then refute-by-default verification of the highest-severity
findings. It produced 75 findings; the 14 verified were all confirmed, none
refuted, and they collapsed to eight distinct defects — every one of which
would have broken a real deployment while `synth` + `cfn-lint` + `cdk-nag`
stayed green.

The discipline that made it worth anything was the verification standard, not
the agent count. Findings were not accepted on an agent's word: the flow-log
defect was proven by decompiling `aws-cdk-lib`'s `vpc-flow-logs.js`, the
missing throttle retry by decompiling `invoke.js`, the security-group egress
defect by decompiling `security-group.ts` *and* quoting the CloudFormation
docs. Three promoted findings were **refuted** on that standard and are
recorded as refuted. And where an agent-named finding was never verified, it
is labelled unverified rather than quietly folded into a fix list.

### Human-in-the-loop operations, by design

Agents did the reversible work. **Every irreversible action passed through a
human**, and that split is enforced by configuration rather than by good
intentions. [`.claude/settings.json`](.claude/settings.json) puts every
`vercel` invocation behind an approval prompt and **denies outright**:

```
vercel deploy --prod*    vercel promote*    vercel rollback*
cdk deploy*              cdk bootstrap*
```

Those five lines are the whole mechanism, and they line up with the project's
written anti-goals: promotion to production is always a human action, and
nothing may mutate an AWS account that does not exist.

The production promotion of 2026-08-27 is the worked example. The agent
provisioned environment variables, resolved every adapter config, ran the
checks, and prepared the deployment — then **stopped and reported READY TO
PROMOTE**. A human typed `vercel deploy --prod`. The agent never held the
ability to promote, and did not ask for it.

The same boundary applies to spend and to secrets. Model escalation was
designed, wired, tested and then **declined on cost by a human**
([ADR 014 §8k](docs/decisions/014-semantic-layer-model-selection.md)) — the
capability finished and deliberately unfunded. Secrets were minted inside a
single subshell, piped straight into `vercel env add` and into a gitignored
file, and **never rendered into a transcript, a log, or a commit**; the
transcript of that session contains variable *names* only.

Why this is worth stating in an engineering README rather than a process
document: it is what makes the rest of the document's claims checkable. An
agent that can promote to production, move money, and read its own secrets
back has no meaningful separation between "reported a result" and "arranged a
result". The gates in this project are only worth something because the agent
running them could not also close them.

