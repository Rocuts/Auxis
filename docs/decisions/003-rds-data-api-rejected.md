# ADR 003 — The RDS Data API rejected

**Status:** accepted (rejection) · **Date:** 2026-08-25 · **Phase:** 1

## Context

The RDS Data API is an HTTP endpoint for running SQL without holding a
database connection. On paper it is the perfect answer to this design's named
bottleneck: request-scoped compute fanning out over many documents exhausts
Postgres connections long before it exhausts CPU, and an HTTP-per-statement
model has no connections to exhaust. It also removes the VPC requirement for
Lambda, which would remove the ENI cold-start penalty and several VPC
endpoints from the CDK stack.

## Decision

**Do not use the RDS Data API.** Use `psycopg` 3 against RDS Proxy on AWS, the
pooled Neon endpoint on Vercel, and the container locally.

## Why

**It is Aurora-only.** The Data API is available for Aurora clusters, not for
RDS PostgreSQL instances. Adopting it therefore means adopting Aurora — which
pulls in [ADR 004](004-aurora-serverless-v2-rejected.md)'s cost and
resume-latency analysis as a side effect of a *driver* choice. A persistence
decision should not silently make a compute decision.

**It would break the single-codebase claim.** The Data API is not a wire
protocol; it is an AWS SDK call with its own request and response shapes,
its own type coercion, and no support for `COPY`, cursors, prepared
statements, or `LISTEN`/`NOTIFY`. There is no `psycopg` in front of it. That
means a *second* `RecordRepository` implementation — not a second adapter over
the same driver, an entirely separate one — with its own type mapping for
`int8range`, `numeric`, and `jsonb`, and its own tests.

That second implementation is the whole problem. This project's central claim
is that one domain runs on three targets because every platform boundary is a
port. A port with three adapters that share a driver is a proven claim; a port
where one adapter reimplements type coercion is a *maintained* claim, and the
AWS adapter is the one that can never be integration-tested here (no account).
An untestable divergent code path is where silent data corruption lives, and
this system's worst failure mode is silent corruption of tax data.

**The bottleneck it solves is already solved.** RDS Proxy multiplexes and
pools connections across Lambda invocations, which is the same problem with
the same shape of answer, at the price of an already-designed resource. The
Data API would buy connection-independence a second time.

## Also weighed

The Data API's per-request pricing and its 1,000-record / 1 MiB result-set
limits would both bind on this workload's bulk inserts, requiring chunking
logic that `psycopg`'s `executemany` does not need.

## When this rejection would flip

If the design ever moves to Aurora for other reasons ([ADR
004](004-aurora-serverless-v2-rejected.md) records what those would be) *and*
the AWS target becomes something that is actually deployed and integration-
tested, the Data API becomes worth re-evaluating — because at that point the
second repository implementation would be testable, and the argument above is
almost entirely about testability.
