# ADR 010 — Vision-OCR as the Vercel `TableExtractor` for scanned input

**Status:** accepted; adapter **built and unit-tested**; a vision model is
**access-probed and wired** as of 2026-08-27; **still no end-to-end run — the
3.5-LIVE seed was meant to settle it and did not** ·
**Date:** 2026-08-25 (addendum 2026-08-27) · **Phase:** 3.5

> Document 05 is extracted by Tesseract in the docker-compose target and by the
> Textract adapter in the AWS design (fixture-tested). The vision adapter is
> built with 26 tests over its fidelity and fail-closed rules, and now has a
> confirmed model behind it — see the addendum at the end of this page.

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

---

## Addendum, 2026-08-27 — the model behind the adapter, probed rather than assumed

The adapter shipped with a hole in the middle of it: it was built, tested
against recorded response shapes, and pointed at **no model that this project
could actually invoke.** Its config chain defaults to the direct Anthropic
route, and this project has no funded direct credential (ADR 014 §8k), so
`README` recorded it honestly as *"the vision-OCR adapter has never run against
a real model."*

**One authorized probe closed that hole.** From the AI Gateway catalogue, 162
of 356 models accept image input. The candidate chosen was not the cheapest but
the one that raises **no new entitlement question at all**:

> **`zai/glm-5.3-flash`** — vision-capable per the catalogue's
> `modalities.input`, and *the same id the mapper ran for all six gate runs*.
> Access on this credential is not a hypothesis; it is six runs of evidence.

The probe rendered a 220x70 PNG carrying `RATE 15.3%` / `OVER $250,000` — the
two token shapes this corpus actually turns on, a percentage and a bounded
currency amount — and asked for a transcription. It returned:

```
RATE 15.3%

OVER $250,000
```

Exact, including the decimal and the thousands separator, for 52 input and 52
output tokens. Access confirmed, and legibility confirmed with it.

### What is now wired, and the trap in the chain

`EXTRACTION_OCR_ENGINE=vision` with `VISION_OCR_MODEL`, `VISION_OCR_BASE_URL`,
`VISION_OCR_API_KEY` and **both** price variables. Two notes worth more than
their length:

1. **The key chain does not include the mapper.** It is
   `VISION_OCR_API_KEY` → `ANTHROPIC_API_KEY`, with no fall-back to
   `SCHEMA_MAPPER_API_KEY`. A deployment whose only credential sits in
   `SCHEMA_MAPPER_API_KEY` therefore fails **closed** at config time on the
   scanned path. That is the correct failure and not a bug — but it is a
   surprise, and it is now documented in `.env.example` next to the variable.
2. **Prices must be set with the model.** A role given a `MODEL` without its
   `USD_PER_MTOK_*` is reported at the Opus defaults; for this id that would
   overstate OCR cost by roughly thirty times. The trap is this project's own,
   already documented for the verifier, and it applies identically here.

### What this does and does not license

It does **not** claim document 05 now maps correctly through the vision path.
Nothing end-to-end has run: extraction fidelity on a full scanned page — merged
cells, the `to` range separator, the footnote-only rate — is a 3.5-LIVE
measurement and is not being asserted in advance. What the probe establishes is
narrower and sufficient: **the adapter has a reachable, legible model behind it,
so the scanned path on the live target is a thing that can be exercised rather
than a hole to be reported.** The pre-registered fail-closed fallback — document
05 landing in the review queue with its provenance — remains the behaviour if
extraction disappoints, which is anti-goal #8 working as designed rather than a
regression.

### The 3.5-LIVE measurement did not happen (2026-08-27)

Document 05 was uploaded to production on the vision path and its job was
killed at the function's `maxDuration` along with the other four, before any
extraction result was written. So the sentence above still stands exactly as
written: **nothing end-to-end has run.** Neither branch — clean vision
extraction, or a fail-closed landing in the review queue — has been observed,
and neither is being reported as though it had been. The measurement is
blocked behind the same promotion as everything else in
[ADR 009's amendment](009-cron-sweep-jobrunner.md); the vision path itself is
not implicated in the failure, which was a clock, not a model.
