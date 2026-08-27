# ADR 010 — Vision-OCR as the Vercel `TableExtractor` for scanned input

**Status:** accepted; **implementation pending Phase 3.5 (gate open)** · **Date:** 2026-08-25 · **Phase:** 3.5

> The adapter is **not yet built**. Document 05 is currently extracted by
> Tesseract (local) or the Textract adapter (the AWS design, fixture-tested).

## Context

Document 05 is a scanned image with no text layer. `pdfplumber` returns
nothing for it, so it must go down the OCR branch of the router
([ADR 006](006-hybrid-extraction-router.md)).

Each target has an OCR adapter: Tesseract locally, Textract on AWS. Vercel has
neither. **Vercel functions cannot install system binaries**, so the Tesseract
executable — and therefore `pytesseract`, which is only a wrapper around it —
cannot exist there. One of five fixture documents would be un-ingestable on
the one target that serves the live URL.

## Decision

A **vision-OCR `TableExtractor` adapter** speaking the **Anthropic Messages
protocol**: render the scanned page to an image and ask a vision-capable model
for the cell grid. The endpoint is configuration, as it is for the semantic
roles (`VISION_OCR_BASE_URL` / `VISION_OCR_MODEL`, defaulting to direct
Anthropic); no such key is funded on this project today, which is why this
adapter has never run against a real model.

## Why this does not bend the no-pixels rule

The rule is easy to state loosely and get wrong, so state it precisely:

> **The rule binds the `SchemaMapper`.** The mapper only ever sees an
> extracted cell grid. `TableExtractor` adapters are the components licensed
> to read pixels.

That licence is not a special exemption invented for Vercel. Textract is
itself an ML-based OCR service — the AWS adapter has always been a model
reading pixels. The vision adapter is its platform equivalent, sitting in the
same port, behind the same interface, producing the same
`PageExtraction` cell grid. Nothing downstream can tell which extractor ran,
which is exactly the property a port is supposed to have.

The invariants that actually matter are unchanged:

- every mapped value still traces to a **cell an extractor produced**;
- the `SchemaMapper` still never reads a number off an image and never invents
  one;
- **the router must never send a document with a usable text layer to the
  vision adapter** — the same test-pinned invariant that keeps four of five
  documents at `$0.00`.

## Why not the alternatives

- **A pure-Python OCR engine** (e.g. an ONNX-based recognizer). No system
  binary, so it would fit — but it means shipping model weights inside the
  function bundle, on a platform where bundle size and cold start are the
  currency, to serve one document. And its accuracy on a low-quality scan is
  the thing that would need proving, with a second accuracy harness.
- **Calling Textract from Vercel.** Cross-cloud credentials in a demo, and it
  would make the "live" target depend on the AWS account this project
  explicitly does not have.
- **Skipping document 05 on Vercel.** Rejected outright: document 05 is the
  one that carries the scanned-input trap *and* the superseded-lifecycle trap.
  A live URL that silently cannot ingest it would be a demo of the easy cases.

## Consequences

Document 05 remains **the only document with nonzero extraction cost on any
target**, and per-document cost stays itemized by role so the claim is
checkable rather than rhetorical.

The adapter is the second consumer of `ANTHROPIC_API_KEY` after the semantic
layer, which keeps the key strictly server-side (anti-goal #10) and means the
Vercel target has exactly one external model dependency rather than two
vendors.

The trade the platform forced is real and worth naming: on Vercel, the OCR
path costs tokens where the local path costs only CPU. The router is what
keeps that from mattering for four documents out of five.
