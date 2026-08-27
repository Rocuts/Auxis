# ADR 015 — Convention-derived discriminators are declared, never dressed as cited

**Status:** accepted · **Date:** 2026-08-26 · **Phase:** 2b

## Context

Every value in a canonical record must trace to a cell or prose block the
extractor produced — that is the traceability contract, enforced structurally
by `check_provenance`. The adversarial pass over document 01 found a field that
cannot satisfy it.

That document is a federal income-tax rate schedule. The strings `federal`,
`Federal`, `United States`, `U.S.`, `USD` and `IRS` appear **nowhere** in its
extracted text. Yet all 32 records asserted `jurisdiction: "US"` and
`currency: "USD"`, citing prose blocks that establish neither. The conventions
license the inference — "US for United States federal documents", "A United
States tax document denominates in USD" — but the provenance array said the
document *stated* it. It did not.

This is a small lie with a large shape: the entire value of the provenance
contract is that a citation means something. A field that quietly borrows a
citation from a neighbouring assertion devalues every honest one.

## Decision

**Convention-derived fields are a declared class.** Where a discriminator is
asserted from the canonical conventions rather than from anything the document
prints, the record names that field in `convention_derived`, and the mapper is
instructed never to manufacture a citation for it.

`convention_derived` is an optional array of field names on the record schema;
`_build_record` carries it into `attrs["convention_derived"]`, so it rides into
the review queue and the fact table's JSONB tail with the record it qualifies.

Three properties, in the order they matter:

1. **Never silent.** The inference is visible in the record itself, not only in
   a prompt a reader would have to go and find.
2. **Never dressed as cited.** A convention is a legitimate source — this ADR
   does not forbid the inference, which is correct on document 01 — but it is a
   *different* source from a printed cell, and the record now says which.
3. **Reviewable as a class.** "Show me every value asserted by convention" is
   one query against the tail, which is exactly the question an auditor of tax
   data should be able to ask.

## Consequences

- The provenance contract regains its meaning: a citation now means the
  document says so, with no exceptions hiding inside it.
- `jurisdiction` on a document that names no jurisdiction, and `currency` where
  no sign or code appears, are the two cases in this corpus. Both are
  legitimate and both are now labelled.
- The README's limitations section carries one line saying so, because a
  reader evaluating the accuracy table deserves to know which fields were
  inferred rather than read.
- A future document that *does* name its jurisdiction produces a cited value
  and an empty `convention_derived`, with no change to anything.
