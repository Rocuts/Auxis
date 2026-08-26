# ADR 006 — The hybrid extraction router: deterministic first, OCR only when unavoidable

**Status:** accepted · **Date:** 2026-08-25 · **Phase:** 2

## Context

The naive architecture for "extract tables from PDFs" in 2026 is to send every
page to a vision model or a managed document-AI service. It is one code path,
it handles scans, and it demos well.

It is also, for this corpus, mostly waste. **Four of the five fixture
documents have a real text layer.** A digital PDF already contains the table's
character positions, and `pdfplumber` reads that structure directly. A model
inferring the same structure from a rasterized image is strictly working with
*less* information, at nonzero cost, nondeterministically.

## Decision

Route per page, on measured page properties:

```
usable text layer, not image-dominated  ->  pdfplumber. $0. deterministic.
scan, or a sideways text layer          ->  the target's OCR adapter
genuinely blank page                    ->  neither. nothing spent.
```

Both branches converge on the same downstream pipeline: extracted cell grid →
mapper → verifier → validators → persist or review queue → adjudicator.

## Why the classifier is a rule and not a model

Anthropic's first published criterion for adding an agent is to find the
simplest thing that works. Here that is a two-branch rule over three
measurable properties: deduplicated upright character count, presence of a
page-sized image, and text orientation. A model in this position would add
nondeterminism, latency, and per-document token cost to a decision that a
threshold makes correctly and reproducibly — with no accuracy left to buy.

Classification uses **raw page evidence only** — never the filename, never
document metadata. Document 05's filename says nothing that document 01's
does not.

## The two invariants, and the bug that produced the second

Both are pinned by tests, one per direction:

1. **A page with a usable text layer is never sent to an OCR adapter.** OCR
   costs money on two of three targets, and the brief's economics argument
   depends on this holding.
2. **A page dominated by a page-sized image is never handed to the
   deterministic adapter** — even when it also carries a small text layer.

The second invariant exists because of a real defect found in adversarial
review. A character-count threshold alone is not enough: a scanner stamp or an
e-file header of 60 characters clears any sane threshold, so a fully scanned
page would classify as digital, `pdfplumber` would find no tables, and the
document would come back **empty at confidence 1.0**. That is silent data loss
wearing a success badge — the worst failure mode this product defines. The fix
is the image-coverage conjunct, and the regression test is a deliberately
stamped scan.

Orientation is decided by character `upright` flags rather than the `/Rotate`
metadata key, because `pdfplumber` resolves page rotation before setting them.
A rotated page whose text reads upright stays on the free deterministic path;
a genuinely sideways text layer, which the deterministic adapter cannot read
reliably, goes to the pixel-licensed port.

## The rule this preserves

The `SchemaMapper` never reads a number off an image. `TableExtractor`
adapters are the only components licensed to read pixels — and Textract is
itself ML-based OCR, so the vision-OCR adapter ([ADR
010](010-vision-ocr-vercel-extractor.md)) is its platform equivalent, not a
loophole. Every mapped value traces to a cell some extractor produced.

## Consequences

The headline number is structural, not a measurement artifact: **four of five
documents cost `$0.00` to extract on every target**, and document 05 is the
only document with nonzero extraction cost anywhere. Per-document cost is
tracked itemized by role so this stays a checkable claim rather than a
slogan.

The cost is two extraction code paths instead of one, and a classifier that
has to be right. Both are covered by tests, and the classifier's failure modes
are now known rather than theoretical.
