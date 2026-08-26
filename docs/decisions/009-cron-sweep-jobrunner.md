# ADR 009 — A cron-sweep `JobRunner` on request-scoped compute

**Status:** accepted; **implementation pending Phase 3.5 (gate open)** · **Date:** 2026-08-25 · **Phase:** 3.5

> The sweep itself is **built and tested** — `POST /internal/sweep`,
> `CRON_SECRET` bearer auth, `sweep_pending()` with `FOR UPDATE SKIP LOCKED`.
> What is pending is the `vercel.json` cron entry that calls it on the
> deployed target.

## Context

`POST /documents` must return **202 immediately** and never block on
extraction — that is an API-design requirement, not an optimization, because
a document with a scanned page and 51 records takes far longer than any
reasonable HTTP timeout.

Locally and on AWS this is easy: an in-process worker pool, or Step Functions.
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

## Why `FOR UPDATE SKIP LOCKED` is the whole design

It is what makes concurrent sweepers safe without a broker. Two overlapping
cron invocations — or a cron and a manual sweep — claim disjoint job sets
rather than contending or double-processing. The same primitive is what lets
the local docker-compose worker pool run several workers against one database.

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

If Queues becomes available, it is a fourth adapter and a `vercel.json`
change. Nothing in the pipeline moves.
