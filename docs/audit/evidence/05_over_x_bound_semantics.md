# Document 05 — the "Over $X" bound-semantics defect, and the layer that caught it

Evidence extracted from the fixture-05 fan-out run's persisted artifacts
(pipeline database, `review_queue` + `records`) before that database's
next reset. Committed because the finding is a README headline and the
container volume is not a deliverable.

Nothing here was read from `fixtures/ground_truth.json`. Every value below
is either printed on the page, produced by the pipeline, or a database
constraint's own error text.

---

## 1. What the page prints

Document 05's rate bands are keyed by a top row that reads, per filing-status
column (Tesseract, page confidence 0.92):

```
"Over $533,400"   single
"Over $600,050"   married_filing_jointly
"Over $300,000"   married_filing_separately
"Over $566,700"   head_of_household
```

Document 01, by contrast, prints the *inclusive* form for its top brackets —
`"$643,251 and over"`, `"$771,901 and over"`, `"$385,951 and over"`,
`"$16,051 and over"`. The corpus therefore contains **both** top-bracket
spellings, and they mean different things.

## 2. What the conventions said, and what the model did

`CANONICAL_CONVENTIONS` defined the open-top forms (`"and over"`, `"or more"`,
`"No limit"` -> `upper_bound` null) and then said, of bounds generally:
*"transcribe, never re-derive."* It said nothing about an **exclusive** lower
bound. So the mapper transcribed:

| filing_status | printed | mapped `lower_bound` | correct under inclusive-bounds |
|---|---|---|---|
| married_filing_separately | `Over $300,000` | 300000 | 300001 |
| single | `Over $533,400` | 533400 | 533401 |
| head_of_household | `Over $566,700` | 566700 | 566701 |
| married_filing_jointly | `Over $600,050` | 600050 | 600051 |

The model obeyed the instruction it was given. All four records carried
mapper confidence **0.94**.

## 3. Every semantic check passed

- **The verifier never ran on this document.** It returned a body with no
  `verdicts` envelope, so all 19 records were flagged `verifier_unavailable`
  rather than trusted (containment, not assent).
- **The adjudicator endorsed the head_of_household record at 0.95**, with
  `citations_valid: true`, engine `zai/glm-5.3-flash`:

  > "…cell p1_t0 r3,c4 reads 'Over $566,700', which transcribes to lower_bound
  > 566700 with upper_bound null (open-ended 'Over'). … the persisted record
  > matches the page, so no change is needed and the item can be dismissed as
  > verified-correct."

- **The mechanical citation check passed too.** `resolution_is_supported`
  asks whether the cited cells carry the figures the resolution asserts. The
  cell literally reads `Over $566,700`, and the resolution asserts 566700.
  **The figure is right; the derivation is wrong.** This check is structurally
  incapable of catching that.

## 4. The database caught all four, with zero access to the oracle

The `EXCLUDE USING gist` constraint refused every one of them at ingest,
because an inclusive `lower_bound` of X collides with the band below, whose
`upper_bound` **is** X:

```
bracket_overlap: bracket [300000, and over] overlaps [48351, 300000] at batch index 6 in the same chain
bracket_overlap: bracket [533400, and over] overlaps [48351, 533400] at batch index 4 in the same chain
bracket_overlap: bracket [566700, and over] overlaps [64751, 566700] at batch index 7 in the same chain
bracket_overlap: bracket [600050, and over] overlaps [96701, 600050] at batch index 5 in the same chain
```

Result: **15 of 19 records persisted, 4 refused, 4 open review-queue rows.**
Nothing was dropped and nothing was guessed (anti-goal #8). The constraint
knew only that two intervals in one chain overlapped — it needed no ground
truth to know the schema had been violated.

## 5. The near miss that ruling 3 closes

The head_of_household record collected **two** queue rows: the
`bracket_overlap` REJECT above, and a `verifier_unavailable` FLAG. The
adjudicator's 0.95 endorsement landed on the FLAG row — and that row was
one rule-name away from closing itself:

- confidence 0.95 >= the 0.9 auto-resolve threshold — **passes**
- `citations_valid` — **passes**
- `resolution_is_supported` — **passes** (§3)
- `_may_auto_resolve` — **blocked**, but only because `verifier_unavailable`
  sits in the verifier-born default-deny set (ADR 014 §8, gate 1)

Had the FLAG been `confidence_floor` instead — an auto-resolvable rule, and
one this very document also produced — the item would have auto-closed with a
full audit trail, marking as "verified-correct" a record **the fact table had
refused to accept**. Eligibility keyed on the rule name, never on whether the
record was actually there.

That is why eligibility now keys on the record's **presence in the fact
table**, and why the adjudicator is no longer told the record was persisted.
