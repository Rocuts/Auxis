# ADR 009 — A cron-sweep `JobRunner` on request-scoped compute

**Status:** accepted; implemented and deployed · **Date:** 2026-08-25,
amended 2026-08-27 · **Phase:** 3.5

> The sweep is built, tested, and live: `POST /internal/sweep` with
> `CRON_SECRET` bearer auth, `sweep_pending()` with `FOR UPDATE SKIP LOCKED`,
> and a one-minute `vercel.json` cron calling it on the deployed target.
>
> **The 2026-08-27 amendment below is the important part of this ADR.** The
> original design was wrong in a way only production could show: it claimed
> `queued` jobs and nothing else, so a worker the platform killed took its job
> down with it and the backstop could not see the failure it exists to cover.
> A lease/visibility timeout fixes it. That fix is written and tested; **it has
> not been deployed** (promotion is a human action), so the live URL still runs
> the queued-only sweep.

## Context

`POST /documents` must return **202 immediately** and never block on
extraction — that is an API-design requirement, not an optimization, because
a document with a scanned page and 51 records takes far longer than any
reasonable HTTP timeout.

Locally and on AWS this is easy: a resident process, or Step Functions.
(Annotated 2026-08-27: the *local* half of that sentence describes a design
option, not this tree. What ships locally is `NullJobRunner` — enqueue only —
and work starts on an explicit authenticated call to `/internal/sweep`. There
is no worker pool in `src/`, so this sentence must not be read as evidence for
anything about concurrent sweepers.)
On Vercel there is no resident process to hand the work to
([ADR 008](008-vercel-as-the-live-target.md)).

## Decision

**A jobs-table sweep driven by a `vercel.json` cron**, at minute granularity.

`POST /documents` persists the document and the job row and returns 202.
`POST /internal/sweep` — authenticated with a `CRON_SECRET` bearer token, so
it is not callable anonymously — claims up to `limit` queued jobs with
`SELECT … FOR UPDATE SKIP LOCKED` and runs each through the shared pipeline.

## Why not Vercel Queues

Queues is the closer analog to the Step Functions Distributed Map fan-out, and
it was the first choice: a message per document, a subscriber endpoint, real
concurrency. It is **not available on this account/team**, so the fallback
CLAUDE.md anticipated is what ships. The trade-off is recorded here rather
than quietly absorbed.

## What the fallback costs, precisely

- **Latency.** Cron granularity is one minute, so a job waits up to ~60
  seconds before processing begins. With Queues it would be near-immediate.
  For a demo where the client polls `GET /jobs/{id}`, this is visible but
  acceptable; for a production ingest SLO it would not be.
- **Throughput is a `limit`, not a fan-out.** One sweep invocation processes
  jobs sequentially up to `maxDuration`. Concurrency comes from overlapping
  sweeps rather than from a broker, which is why `SKIP LOCKED` is load-bearing
  rather than defensive.
- **`limit` and `maxDuration` are therefore one setting in two files.**
  Because the batch is sequential, `limit x (slowest document)` must fit
  inside `maxDuration` with room for provider backoff. Measured, the slowest
  document is 346 s: at `maxDuration=1800` that puts the honest ceiling at
  three, which is what the cron now requests (`/internal/sweep?limit=3`). A
  test reads `vercel.json` and asserts the arithmetic, because a coupling
  spread across two files and nobody's checklist is a coupling that drifts.

## Amendment, 2026-08-27 — claimable is not the same as queued

**What happened.** The Phase 3.5-LIVE seed pushed five fixtures at production.
Every upload was accepted, every job was claimed within a second, and every
worker was then killed by the platform at the 300 s `maxDuration`. A killed
process writes nothing, so five rows stayed `running` forever — and
`sweep_pending` selected `WHERE status = 'queued'`, which made the cron
backstop **blind to precisely the failure it exists to cover**. Worse, a
`running` job reads as live to the SHA-256 idempotency key, so the five
documents could not be re-ingested at all: the retry path was closed by the
same row that recorded the loss.

**The correction.** A job is claimable when it is `queued` **or** when it is
`running` and its lease has expired. That is a visibility timeout, and it is
worth noticing that the AWS adapter never needed one: Step Functions provides
the same guarantee as a platform property. The port's contract — *accepted
work becomes running work, and a lost notification leaves the job recoverable
rather than lost* — was true of the Step Functions adapter and merely asserted
of the cron adapter. The lease is what makes it true of both.

**The invariant, written into the constant rather than into folklore:**

```
JOB_LEASE_SECONDS (default 1860)  >=  maxDuration (1800)
```

A longer lease only delays a rescue. A shorter one is the dangerous
direction — the sweep reclaims a job whose worker is still alive, two workers
map the same document, and the run is billed twice. `JOB_MAX_ATTEMPTS`
(default 3) bounds the other end: a document that reliably kills its worker is
abandoned as `failed` with `error.type == "lease_expired_max_attempts"` rather
than reclaimed forever, because every reclaim spends model credit.

**No migration was needed.** `jobs` already carried `attempt` and `started_at`;
the lease is a predicate over columns that were there for this.

**What is not claimed.** The lease has four tests, including one that fails if
a live worker's job is ever stolen, and it has never run on production. The
five stranded rows are still `running` on the live URL as of 2026-08-27;
`scripts/mark_stranded_jobs.py` closes them to `failed` with an error payload
naming the gate — marking, never deleting, because the rows are the evidence —
and it has not been run either. Both wait on a promotion, which is a human
action.

## Why `FOR UPDATE SKIP LOCKED` is half the design

> **ANNOTATION 2026-08-27.** This section was titled *"…is the whole design"*
> and that was wrong in a way adversarial review caught and production had
> already paid for. **The lock guards the SELECT; a lease predicate guards the
> CLAIM. Both are the design, and only the first was implemented.**
>
> `SKIP LOCKED` holds only for the life of the selecting transaction, and that
> transaction commits the moment the SELECT returns — before any work starts.
> A second sweeper arriving while the first was mid-pipeline therefore re-
> claimed the same job, incremented `attempt`, and processed the document
> concurrently. At the shipped settings — a 60 s cron over documents measured
> at 346 s — that overlap was the steady state, not an edge case. The data
> survived it (re-ingest is a document-scoped atomic replace), but the run was
> billed twice and `attempt` burned twice as fast toward `JOB_MAX_ATTEMPTS`.
>
> `process_job`'s claim now repeats the sweep's own predicate: a job is
> claimable when it is `queued`, or `running` past its lease. The paragraph
> below is left standing rather than rewritten — it states the property the
> project believed it had, and the gap between that and the shipped code is
> the point.

It is what makes concurrent sweepers safe without a broker. Two overlapping
cron invocations — or a cron and a manual sweep — claim disjoint job sets
rather than contending or double-processing.

Job-level idempotency is enforced by the database, not by the sweeper:

- a partial unique index makes **a second live job per document
  unrepresentable**, so the race between "check for a live job" and "insert
  one" is settled by the index rather than by application logic;
- a document whose latest job **succeeded** is not reprocessed — re-uploading
  the same PDF is a no-op end to end, on top of the SHA-256 document key;
- a document whose latest job **failed** gets a fresh job, which is the retry
  path after an outage.

## Honesty about failure

A job accepted without usable mapping credentials is **not** an HTTP error —
the upload was valid, the service is misconfigured. So the *job* fails, with
`error.type == "missing_credentials"`, visible on `GET /jobs/{id}`. Error
payloads carry exception class names and messages that may name environment
variables, never their values (anti-goal #10).

## Consequences

The `JobRunner` port now has three adapters that are genuinely different
mechanisms — in-process pool, cron sweep, Distributed Map — behind one
interface whose entire contract is "accepted work becomes running work, and a
lost notification leaves the job recoverable rather than lost." That contract
is what let the AWS adapter's `notify()` be written to never raise past the
202 the client already earned.

The amendment above is the contract being *audited* rather than assumed: the
cron adapter satisfied it for a dropped kick and not for a killed worker, and
the gap was invisible until a platform killed one. A port's contract is only
as good as the weakest adapter behind it, and the weakest adapter is the one
running on the target you actually deployed.

If Queues becomes available, it is a fourth adapter and a `vercel.json`
change. Nothing in the pipeline moves.
