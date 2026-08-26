# ADR 004 — Aurora Serverless v2 rejected, with the threshold at which to migrate

**Status:** accepted (rejection) · **Date:** 2026-08-25 · **Phase:** 1

## Context

The database holds 128 records. Paying for a database instance around the
clock to serve a demo is obviously wasteful, and Aurora Serverless v2 with a
minimum capacity of 0 ACUs — scale to zero, auto-pause, auto-resume — is the
purpose-built answer.

## Decision

**Do not use Aurora Serverless v2** for either the AWS design or the live
demo. The AWS design uses a provisioned RDS PostgreSQL instance behind RDS
Proxy; the live demo uses Neon.

## Why

**1. Auto-pause and RDS Proxy are mutually exclusive.** This is the decisive
one, and it is not a latency trade-off — it is a contradiction. AWS documents
it directly:

> "If your Aurora cluster has an associated RDS Proxy, the proxy maintains an
> open connection to each DB instance in the cluster. Thus, any Aurora
> serverless instances in such a cluster won't automatically pause."

RDS Proxy is in this design because connection exhaustion under Distributed
Map fan-out is the README's named bottleneck #3. So Serverless v2's headline
benefit is unavailable in exactly the architecture that would use it. Choosing
it would mean paying the serverless premium for a cluster that never pauses.

**2. Even without the proxy, the resume latency breaks a demo URL.** Again
verbatim:

> "Because the typical time to resume might be approximately 15 seconds, we
> recommend that you adjust any client timeout settings to be longer than 15
> seconds."

and, after a day of idleness:

> "If an Aurora serverless instance remains paused more than 24 hours, Aurora
> can put the instance into a deeper sleep that takes longer to resume. In
> that case, the resume time can be 30 seconds or longer."

A live URL that an evaluator opens cold, once, after it has sat idle for days,
is the *worst* case for this feature. First request takes 15–30+ seconds on
top of function cold start, or times out. The URL looks broken. No amount of
architectural elegance survives that first impression.

**3. Keeping the floor above zero costs more than the workload is worth.**
Holding `MinCapacity` at 0.5 ACU to avoid the resume cliff costs, at us-east-1
list price, `0.5 ACU × $0.12/ACU-hour × 730 hours ≈ **$43.80/month**` — per
instance, before storage, I/O, and before Multi-AZ doubles it. For a 128-record
database that is not a trade-off; it is a category error.

## The migration threshold — when this rejection flips

Serverless v2 becomes the correct choice when **all three** of these hold:

1. **Load is genuinely bursty**, with a peak-to-average ratio high enough that
   a fixed instance must be sized for the peak and idles through the trough.
   Concretely: when sustained average consumption exceeds roughly
   `$47/month ÷ $0.12/ACU-hour ÷ 730 hours ≈ 0.54 ACU` *and* the peak needs
   several times that. Below that, a `db.t4g.medium` is cheaper and has no
   resume cliff.
2. **The connection-pooling requirement is met another way** — a client-side
   pooler, or a workload that no longer fans out — so that RDS Proxy can be
   dropped and auto-pause becomes reachable at all.
3. **No user-facing request can be the one that resumes the cluster.** A
   queue-fronted, entirely asynchronous ingest path satisfies this; a
   public `GET /records` does not.

For this project, at 10,000 documents/day, (1) plausibly becomes true while
(2) does not — the fan-out is the reason the proxy exists. That is the honest
summary: the workload that would justify Serverless v2 is the same workload
that prevents it from pausing.

## Addendum (2026-08-26) — the comparator, measured on both sides

This ADR was written against a documented figure on one side and nothing on
the other. Phase 3.5 supplied the missing half: the chosen stack's true first
click — cold function plus **Neon resuming from autosuspend** — was measured by
leaving a preview deployment untouched for 430 s (past Neon's 5-minute
autosuspend) and then issuing exactly one request to a data-path endpoint.

**6.76 s end to end**, of which 4.42 s is server-side and roughly **3.9 s is
the database waking**; warm requests on the same endpoint run 0.37–0.43 s.

That result **weakens one claim in this ADR without changing its conclusion.**
The rejection reasoned that a ~15 s resume "breaks a demo URL", carrying an
implicit assumption that the alternative was in a different class. It is not:
resume-to-resume the comparison is **~3.9 s vs ~15 s (about 3.8×)**, and first
click to first click **6.76 s vs ~15 s (about 2.2×)**, widening to roughly
4.5× against the documented 30 s+ that Serverless v2 costs after 24 h of
idleness. A 2–4× margin is decisive for this decision — 6.76 s reads as slow
where 15 s reads as broken, and the deeper-sleep case is worse still — but it
is not the order of magnitude the reasoning implicitly assumed.

The honest generalization, which the original text did not state: **every
scale-to-zero database has a first-click cliff**, and this design has one too.
It chose a smaller cliff, not the absence of one. What actually makes the
decision safe is not the size of the gap but the two independent arguments
above it — that RDS Proxy and auto-pause are mutually exclusive, so Serverless
v2's headline benefit is unavailable in this architecture at all, and that
holding `MinCapacity` above zero costs ~$43.80/month for a 128-record
database.
