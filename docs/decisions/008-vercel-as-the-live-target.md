# ADR 008 — Vercel as the live demo target

**Status:** accepted; **implementation pending Phase 3.5 (gate open)** · **Date:** 2026-08-25 · **Phase:** 3.5

> This ADR records a decision that is made and settled. The Vercel adapters
> are **not yet built** — there is no `vercel.json`, no vision-OCR adapter, and
> no live URL. The README says so plainly in its three-targets section. The
> parts already built and tested are the cron-sweep `JobRunner`
> ([ADR 009](009-cron-sweep-jobrunner.md)) and the `bytea` blob store
> ([ADR 011](011-blob-in-postgres-vs-vercel-blob.md)).

## Context

The brief asks for a **live service URL**. The AWS design cannot supply it
(no account, no budget — the stack synthesizes and is never deployed), and
`docker compose` is a reproduction, not a URL. Something has to actually run
on the public internet.

## Decision

**Vercel**, running the FastAPI app on Fluid compute, with Neon Postgres via
the Vercel Marketplace integration.

## Why

- **It is named in the brief itself**, and the account is already licensed
  (Pro). A hosting decision that needs no procurement conversation is the
  right hosting decision for a take-home.
- **Python and FastAPI are first-class.** Vercel builds consume
  `pyproject.toml` and `uv` natively, so the same lockfile that produces the
  test environment produces the deployment. No second dependency manifest.
- **Fluid compute reuses function instances across concurrent requests**,
  which materially reduces the cold-start penalty that is the whole reason
  Aurora Serverless v2's resume latency was rejected
  ([ADR 004](004-aurora-serverless-v2-rejected.md)). Rejecting a 15-second
  database resume and then shipping a 15-second function cold start would be
  incoherent.
- **Git integration gives a preview deployment per PR**, so the deployment
  path is exercised before anything is promoted — and promotion to production
  stays a human action (anti-goal #9).
- **Neon is standard Postgres**, including `btree_gist`. That is not
  incidental: the exclusion constraint in [ADR 001](001-postgresql-engine.md)
  is the data model's centrepiece, and a "Postgres-compatible" host that
  lacked the extension would silently demote it. `CREATE EXTENSION btree_gist`
  on the Neon branch is part of the Phase 1 gate, not an assumption.

## What it costs — the constraints that shape the adapters

Choosing request-scoped compute has consequences, and they are the reason
three of the other ADRs exist:

- **No resident worker process.** The in-process `JobRunner` cannot serve the
  live URL, so `POST /documents` must return `202` and processing must happen
  elsewhere — [ADR 009](009-cron-sweep-jobrunner.md).
- **No system binaries.** Tesseract cannot be installed into a function, so
  the scanned document needs a different extractor —
  [ADR 010](010-vision-ocr-vercel-extractor.md).
- **Connections are per-invocation.** Neon's *pooled* endpoint is mandatory,
  and `psycopg`'s prepared-statement behaviour has to be verified against the
  pooler (`prepare_threshold` if needed) — a Phase 1 gate item.
- **Bundle hygiene.** `excludeFiles` keeps `fixtures/`, `tests/`, and docs out
  of the function bundle. This is not only size: it means **the ground truth
  never ships in the deployed artifact**, which reinforces anti-goal #1 at the
  packaging layer rather than only by convention.

## Alternatives

- **A long-running container host** would fit the in-process `JobRunner`
  exactly. Rejected because free container tiers cold-start after idle,
  reproducing the *precise* failure mode [ADR
  004](004-aurora-serverless-v2-rejected.md) rejected: an evaluator opens the
  URL cold and it looks broken.
- **Deploying the AWS stack** is forbidden (anti-goal #5): no account, no
  budget, and the README must never imply an AWS deployment that does not
  exist.

## Consequences

The live URL runs the Vercel adapters and nothing else changes: the domain,
pipeline, validators, and repository are the same code the test suite and the
CDK stack use. That is the hexagonal claim being *paid for* rather than
asserted — the platform constraints above all land on adapters, and none of
them reached the pipeline.
