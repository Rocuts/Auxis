# ADR 011 — Blob-in-Postgres (`bytea`) over Vercel Blob

**Status:** accepted · **Date:** 2026-08-25 · **Phase:** 3.5

> The `document_blobs` table and its adapter are **built and tested**; what is
> pending is the deployment that uses them ([ADR
> 008](008-vercel-as-the-live-target.md)).

## Context

The original PDF bytes must be kept. Provenance is a first-class requirement —
`GET /documents/{id}` exists so a reviewer can get back to the source — and
the async pipeline needs to re-read the document some time after the request
that uploaded it has ended. On request-scoped compute there is no local disk
to leave it on.

The `BlobStore` port already has an S3 adapter (AWS) and a Postgres `bytea` adapter
(local). The question is what the Vercel target uses.

## Decision

**Postgres `bytea`**, in a dedicated `document_blobs` table:

```sql
CREATE TABLE document_blobs (
    document_id uuid PRIMARY KEY REFERENCES documents (id) ON DELETE CASCADE,
    content     bytea NOT NULL
);
```

Bytes live in their own table rather than as a column on `documents`, so
listing documents never drags megabytes per row — the metadata table stays
cheap to scan and the blob is fetched only when something actually needs the
PDF.

## Why

- **One store, one transaction, one lifecycle.** Registering a document and
  storing its bytes commit together. With an external blob store, the two can
  diverge: a row with no object (the pipeline fails on a document that
  "exists"), or an object with no row (an orphan nothing will ever delete).
  For a corpus of five documents, buying a distributed-consistency problem to
  save a few megabytes is a bad trade.
- **`ON DELETE CASCADE` is the entire retention policy.** Deleting a document
  deletes its bytes, atomically, with no lifecycle rule and no reconciliation
  job.
- **No extra service, no extra credential, no extra failure mode** on the
  target whose whole job is to be a working URL.
- **The corpus is tiny.** Five fixtures, the largest ~400 KB. `bytea` handles
  this without noticing.

## Why not Vercel Blob

Vercel Blob is the right primitive at real volume — content-addressed
storage, CDN delivery, no database bloat, and it does not put multi-megabyte
values through the connection pool. It is rejected here purely on scale: at
this corpus size it adds a second store, a second credential, and the
two-phase consistency problem above, in exchange for nothing measurable.

## The threshold — when to switch

Switch to an object store (Vercel Blob, or S3 as the AWS target already does)
when any of these become true:

1. **Total blob volume approaches ~1 GB, or single documents exceed a few
   MB.** `bytea` values are TOASTed and read through the same connection the
   query uses; large values inflate backup size, restore time, and pooled
   connection hold time — and connection hold time is
   [the named bottleneck](../../README.md#parallel-processing-and-bottlenecks).
2. **The README's 10,000 documents/day target is actually pursued.** At up to
   10 MB each that is up to **100 GB/day**, which is not a Postgres question.
   This is the concrete number: the blob adapter is correct for a demo corpus
   and wrong for that workload, and the switch is a `BlobStore` adapter swap,
   not a redesign.
3. **Clients need to download originals directly.** Serving a PDF through the
   API means reading it into function memory; an object store issues a signed
   URL and the bytes never touch the compute.

## Consequences

The AWS target already uses S3, so the port has a real object-store adapter
today — the migration path in (1)–(3) is exercised code, not a plan. That is
the payoff for having made `BlobStore` a port in the first place: the scaling
decision is deferred to the moment there is data to make it with, and costs
one adapter when it arrives.
