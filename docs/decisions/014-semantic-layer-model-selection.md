# ADR 014 — Semantic-layer model selection, and its pre-registered escalation rule

**Status:** accepted · **Date:** 2026-08-26 (amended same day: §4 carve-out implemented, §5 venue fixed, §6 outcome recorded) · **Phase:** 2b

## Context

The semantic layer is three model roles (ADR 012). Which model they run on was
settled by cost — `zai/glm-5.3-flash` through the Vercel AI Gateway maps a full
five-document run for roughly $0.009 against ~$0.83 on Opus 5, a 91× difference
— and the ports design absorbed the switch as pure configuration.

Cost is a legitimate criterion, but it is not evidence of fitness. A cheap model
that maps tax brackets wrongly is not a saving. Two properties therefore have to
be measured, and the decision rule that acts on them has to exist **before the
first number does**, or the choice of model becomes a search for a run that
flatters the model already chosen.

That is the entire purpose of this page: to fix the rule, the thresholds, and
the categories in advance.

## Decision

### 1. Role assignment

| Role | Model | Input / output, USD per Mtok | Cache read |
|---|---|---|---|
| `SchemaMapper` | `zai/glm-5.3-flash` | 0.075 / 0.25 | 0.2× input |
| `RecordVerifier` | `alibaba/qwen-3-235b` | 0.22 / 0.88 | no published discount → full rate |
| `Adjudicator` | inherits the mapper | 0.075 / 0.25 | 0.2× input |

Prices are the gateway catalogue's for those exact ids, read 2026-08-26 from
`GET https://ai-gateway.vercel.sh/v1/models`. They are set explicitly per role:
the config chain transfers the mapper's prices to another role **only** while
that role runs the mapper's model, so a verifier pointed at qwen without its own
prices would have been reported at the Opus defaults — 22× its real rate.

The verifier is deliberately a **different model family from the mapper**. ADR
012 names `RECORD_VERIFIER_MODEL` as the mitigation for Anthropic's conformity
finding ("when one agent makes a bad decision, it is likely that many agents
will make that same bad decision"), and a same-model verifier leaves that
mitigation available but unused. Cross-family independence costs $0.0008 per
run here, which is not a trade-off worth deliberating.

### 2. Conformance is measured, not asserted

The gateway forwards the adapters' `output_config` json_schema request without
enforcing it for a non-Anthropic model: the contract is honoured by the model's
instruction-following, not by the transport. The adapters already fail closed —
a `MapperError`, never a persisted guess — so the exposure was never silent
corruption. What was missing was a rate, and `observability/conformance.py` now
produces one per role, printed beside the accuracy table:

- **`schema_failures`** — the call yielded no usable body: invalid or
  incomplete JSON, a missing `records`/`issues` envelope, a truncated or
  refused generation. This is the failure that costs a document its records.
- **`malformed_items`** — the envelope held and an item inside it did not: a
  proposed record failing canonical validation, a verdict naming a record
  outside the batch, a record the verifier silently skipped. A model-authored
  issue about a cell it could not read is **not** counted: raising that issue
  is the contract working.
- **`retries`** — retryable HTTP responses (408/409/429/5xx) the SDK absorbed.
  Reported beside the rates and never folded into them.

### 3. The escalation rule, pre-registered

`SCHEMA_MAPPER_MODEL` escalates from `zai/glm-5.3-flash` to the gateway's
claude-haiku-class id, `anthropic/claude-haiku-4.5` ($1.00 / $5.00 per Mtok,
Anthropic's 0.1× / 1.25× cache ratios), and the Phase 2b gate re-runs, if the
gate run trips **either** trigger:

**Trigger A — mapping attribution.** At least one field-level miss in the
accuracy comparison whose root cause is attributed to the mapper's semantic
judgment: the value was present and unambiguous in the extracted grid or prose
the mapper received, and neither an extraction defect nor the harness's
comparison rules explain the difference. Threshold: **≥ 1**. It is deliberately
this strict because the gate target is 128/128 — a mapping-attributed miss
already fails the gate, so a looser threshold would only license shipping a
known-worse model.

**Trigger B — conformance.** Measured by the ledger over the five-document run:

- **B1** — hard contract failures, **> 0**. One is enough: a document that
  loses its mapping loses its records, and the gate fails with it.
- **B2** — item-level malformed rate **> 2%** of proposed records (≥ 3 of 128).
  Each malformed proposal is a review-queue item a human must clear; three
  across five documents is a model that does not reliably emit the contract.

**Explicitly not triggers:**

- **Throttling.** A 429 the SDK retried through, or a run that dies on an
  exhausted retry budget, is a throughput event. It is re-run, never escalated:
  it says nothing about whether the model can emit the schema.
- **Envelope residue.** A response whose JSON value is complete and correct and
  whose only other content is markdown fence framing. Its rate is measured and
  printed beside the accuracy table, and it is deliberately excluded from B1:
  the contract was met and the presentation was not, and escalating a model for
  formatting its correct answer would be paying for a different failure than
  the one observed. §4 fixes the boundary.

Escalation is **one step**. Going further (to `anthropic/claude-sonnet-5` or
`claude-opus-5`) is an operator decision, not a rule this page grants.

Both runs' tables — accuracy and conformance, before and after — ship in the
README as model-selection evidence. A single table showing the model that
happened to be chosen is an assertion; two tables with a rule written before
either of them is evidence.

### 4. The residue carve-out, scoped and implemented

**This rule was written after one look at real output, and says so.** A
single-document smoke test of the instrumentation (fixture 02, dry, nothing
persisted) returned a body that `json.loads` parsed completely and then
rejected with `Extra data`: `stop_reason='end_turn'`, a 7,880-character valid
object carrying all 8 of that document's records, followed by a stray
two-backtick remnant. The semantic content conformed; the envelope carried
transport residue from a gateway that forwards `output_config` without
enforcing it. Recording that here rather than folding it quietly into a
threshold is the point of pre-registration.

`adapters/envelope.py` accommodates that framing at the transport boundary,
under a rule narrow enough to state in one sentence:

> A body is accepted when **exactly one complete JSON value parses** and the
> only other content is **fence framing** — an optional leading fence line (a
> backtick run, optionally with a language tag, on its own line), an optional
> trailing backtick run, and whitespace.

Everything else stays a hard contract failure: prose before or after the value,
a second value, a truncated value, an empty body, backticks with content on the
same line. The distance between "strip fence characters" and "salvage what you
can" is the distance between a transport fix and silent data invention, and the
rejection cases are tested first for that reason. When the framing turns out not
to have been the whole problem, the **strict** error is re-raised, so a
traceback always describes what the model actually sent.

**Never silent.** Every accommodation increments a residue counter by role and
by position (leading / trailing), and the rate prints beside the accuracy table
with the positions broken out. An accommodation nobody can see is a repair; one
that shows up as a published rate is a documented property of the model. That
visibility is the whole justification for permitting it at all — and it is why
residue is excluded from the escalation trigger rather than quietly counted as
success.

### 5. Escalation goes direct to Anthropic, never through the gateway

The venue is fixed **before** the run, for the same reason the thresholds are.
It is not a formality: the gateway route is reachable-looking and is the wrong
answer, for reasons that are easier to weigh now than under a red gate.

The free tier's price ceiling makes the gateway route unavailable in any case —
`anthropic/claude-haiku-4.5` returns 403 on the configured key while cheap ids
invoke, and the $4.999 balance is the recurring monthly allowance rather than
purchased credit. The dev-log entry for 2026-08-26 carries the measured
probe table and the policy history.

**Route.** The operator funds $5 at `console.anthropic.com`;
`ANTHROPIC_API_KEY` becomes a direct `sk-ant-…` key; `SCHEMA_MAPPER_BASE_URL`
is unset; `SCHEMA_MAPPER_MODEL` becomes the claude-haiku-4.5-class id. The
adapters already read that chain, so it is an environment flip and no code
change — the ports design paying out again.

**Why not simply buy gateway credits:**

1. **A gateway purchase is irreversible in the wrong direction.** The first
   credit purchase permanently ends the recurring monthly allowance. Spending
   $5 there costs $5 *and* the standing $5/30 days.
2. **The direct API enforces structured outputs.** It removes the conformance
   caveat this whole ADR is built around, in the same move that escalates the
   model — §4's carve-out becomes unnecessary rather than merely measured.
3. **The price is the same.** The gateway applies no markup, so the direct
   route costs list price exactly as the gateway does.
4. **It is one env flip**, already exercised: the adapters were built
   dual-route from the start.

`anthropic/claude-3-haiku` — the only Anthropic-family id reachable on the free
tier — stays a **mechanism** fallback: it would prove the escalation path runs.
It is a March 2024 model and may never be the source of a reported result.

### 6. Outcome of the pre-registered run — the PRE-HARDENING BASELINE

Run 2026-08-26 on the reachable pair. Full tables in the dev-log entry of the
same date.

| Trigger | Threshold | Measured | Fired |
|---|---|---|---|
| A — mapping attribution | ≥ 1 semantic miss | **0** | **No** |
| B1 — hard contract failures | > 0 | **3** (2 mapper, 1 verifier) | **Yes** |
| B2 — malformed item rate | > 2% | **72.9%** (51/70) | **Yes** |

Field-level accuracy **0/128**, with `diff` 0 and `extra` 0 — every miss is a
record that never reached the comparison, not a value mapped wrongly. Five
adversarial passes, each denied the oracle, refuted no mapped value; on document
03 repairing envelope faults alone yields 51/51 valid records with no semantic
correction.

Two things the rule did not anticipate and which the record should carry:

1. **The verifier needs escalating too.** Document 04 produced 19 cleanly
   conformant records — the only such response of the run — and was then lost
   because `alibaba/qwen-3-235b` returned a body without the `verdicts`
   envelope. Delta 2 bought cross-family independence and paid for it in
   conformance. Escalation must cover both roles, not just `SCHEMA_MAPPER_MODEL`.
2. **The written thresholds held up under a result nobody wanted.** B1 and B2
   fired on measurement; A did not, and it would have been easy to read 0/128 as
   a semantic catastrophe if the categories had not been fixed beforehand. That
   is the whole return on pre-registering.

**Escalation is BLOCKED BY OPERATOR CONSTRAINT, not waived.** The §5 route
requires funding at `console.anthropic.com`; that budget does not currently
exist. The trigger fired, the remedy is identified, and it is unavailable — a
distinction worth keeping precise, because "we chose not to escalate" and "we
could not" are different statements about this project and only the second is
true.

**§6's both-roles gap is confirmed.** The rule as pre-registered named only
`SCHEMA_MAPPER_MODEL`. Document 04 showed that insufficient: it produced 19
cleanly conformant records — the run's only such response — and lost them to
the verifier's own contract failure. Any escalation must move both roles.

### 7. The hardening pass — the primary remediation

With the escalation route budget-gated, the pre-registered hardening pass
becomes the primary remedy rather than a fallback: **one pass, one gate
re-run.** Its premise is the baseline's own finding — the semantics were
already right, so the work is entirely at the contract boundary.

1. **Schema minimization.** Every field the pipeline already knows leaves the
   model's required surface. `source_page` is no longer asked for at all (it is
   injected from the extraction, which assigned the table id and knows its
   page); `table_id` falls back to the record's own provenance citations.
   Required keys shrink from 20 to 15 — the true semantic core, being what
   only a reader of this document can supply.
2. **A fixed `attribute_key` vocabulary** per record type, enumerated from the
   labels these documents print. A free-form slug drifted between runs of the
   same document, which breaks natural-key matching and idempotency alike.
3. **A bounded envelope adapter, closed list, both roles.** Object-shaped
   `extra_attrs` becomes pairs; a quoted number becomes a Decimal; fence
   framing is stripped per §4. **Nothing else.** Prose after the JSON, a second
   value, a missing semantic field, a non-numeric string in a numeric slot —
   all remain hard contract failures, and the rejection cases are tested first.
4. **Two bounded retries** per document per role, justified by the measured
   per-call failure rate rather than by hope, and **counted per attempt** so
   retrying depresses the conformance rate rather than hiding behind it.
5. **Verifier failure containment.** A verifier that cannot answer after its
   retries no longer costs the document: its records persist flagged under
   `verifier_unavailable`, and the report states verified and
   flagged-unverified counts distinctly. Silence is still never assent.
6. **The prior-year shape settled** — one record per item, prior year as an
   attribute — and per-document record counts printed, so a proposal delta is
   explained rather than carried.
7. **Convention-derived discriminators declared** ([ADR 015](015-convention-derived-discriminators.md)).
8. **Database isolation**, so the contamination that spoiled the baseline's
   persistence data is unrepresentable rather than merely regretted.

The adaptations in (3) are counted and printed exactly as residue is, and for
the same reason: **a repair is not compliance.** The conformance table reports
them beside the rates, never inside them, so a hardened run cannot flatter the
model by absorbing its deviations invisibly.

If the hardened gate lands short of 128/128 it ships truthfully with every
failing record named, and the budget conversation reopens with data.

### 8. Outcome of the hardened run

Run 2026-08-26, same models, after the §7 pass. Full tables in the dev-log
entry of the same date.

| Measure | Baseline | Hardened |
|---|---|---|
| Records delivered | 0 of 130 proposed | **128** (32/8/51/18/19 — the oracle's exact per-document counts) |
| mapper `schema_fail` | 2 | **0** |
| mapper `item_ok` | 27.1% | **100%** |
| Closed-list adaptations needed | n/a | **0** |
| Verifier | 1 hard failure, document lost | **answered on all five**, 0 flagged unavailable |
| Throttling | 191 retryable | **0** |
| Field-level accuracy | 0/128 (`miss 128, extra 0`) | 0/128 (**`miss 128, extra 128`**) |

**The hardening closed what it targeted, completely.** The conformance layer is
clean on every axis, and the two fence-framed responses were stripped and
reported rather than absorbed silently.

**The accuracy failure is now a different thing entirely.** `extra 128` means
every record arrived; none matched a natural key. The cause is four string
fields where this repository's `CANONICAL_CONVENTIONS` and the oracle's target
schema disagree — `jurisdiction` (`US` / state names vs `US-FED` / `US-XX`
codes), `taxpayer_class` (null vs explicit `individual`; `estates_and_trusts`
vs `estate_or_trust`), and four abbreviated `attribute_key` slugs. Exactly one
genuine data-level discrepancy survives that analysis.

**Trigger re-evaluation.** A (mapping attribution): the misses are not semantic
judgment but a naming-convention mismatch, so it does **not** fire on the
pre-registered wording. B1: **0**. B2: **0**. So the hardened run fires no
escalation trigger — which is the correct reading, because escalating the model
would not move a single one of these records: the model followed the
conventions it was given, and the conventions are wrong about four spellings.

**The fork, RESOLVED by operator ruling — and the boundary that resolves it.**

> **Encoding vocabularies documented in the target schema are adoptable.
> Per-record extracted values are not. `src/` never opens the oracle at
> runtime.**

The line between them is an *extractability test*: **`US-FED` prints in no
PDF.** It cannot be extracted, only agreed; it is an encoding of a fact, not
the fact. Alabama's `4.000` rate, by contrast, is printed on the page and is
the answer the harness exists to check — deriving it from the oracle would be
answer-copying, and remains forbidden.

The precedent was already in the repository and had simply not been named: the
`RecordType` and `FilingStatus` enums are target vocabularies, not extracted
strings, and migration 0005 added `qualifying_surviving_spouse` for exactly
this reason. Adopting `US-FED`, `individual`, `estate_or_trust` and four
`attribute_key` slugs is the same act.

`CANONICAL_CONVENTIONS` is corrected accordingly, and the conventions now say
plainly that `jurisdiction` is an encoding — `US-FED`, or `US-` plus the ISO
3166-2 subdivision code — rather than the printed string.

**A blind spot this run exposed, and the two gates that answer it.** Document
01 produced zero contract failures and two genuine semantic defects — a
false-positive verifier dispute, and an adjudicator that auto-resolved at 0.98
while repeating the disputer's false premise about a record whose real value it
had been given. Every response involved was perfectly conformant. **The
conformance ledger is structurally blind to substantive wrongness**; it
measures whether the model can emit the contract, never whether what it emitted
is true. That is the harness's job, and only the harness's.

Two gates now stand in front of an unattended close:

1. **Dispute-born items join REJECT-born in default-deny** —
   `AUTO_RESOLVABLE_RULES` is strictly narrower than `FLAG_RULES`, excluding
   `verifier_dispute` and `verifier_unavailable`. A dispute is a *second*
   opinion that something is wrong; a *third* model agreeing with it is
   correlation, not corroboration — the same conformity risk ADR 012 names.
2. **An auto-resolution must be mechanically supported by its own citations.**
   `citations_valid` proves only that the cited cells exist;
   `resolution_is_supported` asks whether they carry the figures the resolution
   states. Every asserted number must appear in cited evidence or be reachable
   by one of the two transforms this schema documents (percent to fraction; a
   bracket bound derived by one). Grid coordinates are excluded as addresses
   rather than claims. Fail-closed: anything further away goes to a human.

### 8a. The target-contract boundary, and its second application

§8's ruling — **encoding vocabularies documented in the target schema are
adoptable; per-record extracted values are not; `src/` never opens the oracle
at runtime** — resolved the four string fields. The fixture-05 fan-out then
produced a case testing the same line from the numeric side, and the operator
ruled it the same way.

Document 05 prints its top bands as `Over $533,400`, `Over $600,050`,
`Over $300,000`, `Over $566,700`. Document 01 prints its top brackets as
`$643,251 and over`. `CANONICAL_CONVENTIONS` defined the second form and not
the first, and closed with *"transcribe, never re-derive"* — so the mapper
emitted `lower_bound 566700` for `Over $566,700`, which under an
**inclusive**-bounds schema claims the band contains 566,700, a figure the
band below already claims as its `upper_bound`.

**The rule adopted.** An exclusive lower-bound phrase (`Over $X`,
`More than $X`, `Above $X`, `In excess of $X`) stores as `lower_bound = X + 1`
in whole currency; the inclusive forms (`$X and over`, `$X or more`,
`at least $X`) store as `lower_bound = X`.

**Why this is inside the boundary, not outside it.** The figure is not in
dispute: `566,700` is printed on the page and is transcribed unchanged. What
the rule fixes is the schema's **encoding of an exclusive phrase into an
inclusive slot** — the same class of act as encoding an open top as
`upper_bound null`, which these conventions have always declared. `+ 1` is
not extractable from the page any more than `US-FED` is; it follows from the
target schema's stated inclusive-bounds convention. So `"transcribe, never
re-derive"` is narrowed in writing to what it always meant — **it binds
figures** — and bound *semantics* join the open-top forms as the enumerated
exception. No oracle value was copied, and none was needed: the constraint
that exposed the defect is in the DDL, not in the ground truth.

**What this cost, and what caught it.** Four records, all four rejected by
the `EXCLUDE USING gist` constraint at ingest, all four queued open. Every
semantic check upstream had passed them — mapper confidence 0.94, the verifier
unavailable on that document, and the adjudicator endorsing the
head_of_household record at **0.95 with `citations_valid: true` and a
mechanically supported citation**, because `Over $566,700` really does contain
the figure 566700. The citation check validates *figures against cells*; it
cannot see a wrong *derivation* from a right figure. Full evidence in
[docs/audit/evidence/05_over_x_bound_semantics.md](../audit/evidence/05_over_x_bound_semantics.md).

**And a latent hole it exposed.** That endorsed record was **absent from the
fact table** — the constraint had refused it — yet its `verifier_unavailable`
FLAG row cleared threshold, citations, and mechanical support. It stayed open
only because gate 1 above default-denies verifier-born rules. Had the flag
been `confidence_floor` — auto-resolvable, and a rule this same document
produced — the item would have auto-closed as "verified-correct" over data the
database had rejected. Eligibility keyed on the queue row's **rule name**; it
now keys on the record's **presence in the fact table**, which is the property
that was always meant (gate 3, §8b).

### 8b. Auto-resolve eligibility keys on presence, not on rule name

`_may_auto_resolve` asks the repository whether the record the queue row
carries is actually in the fact table, and default-denies when it is not, when
the row carries no record at all (a mapping issue), or when presence cannot be
determined. The rule-name filter of gate 1 is *retained on top* of it: the two
gates are independent, and a verifier-born item stays human-only whether or not
its record persisted.

This closes the class rather than the instance. A REJECT and a FLAG can land on
the same record — triage runs every rule over every record — so no enumeration
of *reasons* can be sound. Presence is the property that actually licenses an
unattended close: closing a row whose record is in the table loses nothing,
because the row's own record is still there to be re-examined; closing a row
whose record is absent destroys the only live signal of the loss (anti-goal #8).

## Consequences

- The model choice is now falsifiable: a rule, two thresholds, and two tables.
- Cross-family verification is applied rather than merely configurable, which
  is ADR 012's conformity mitigation actually paid for.
- Cost lines are correct at first print for every role, including a verifier on
  a provider that publishes no cache discount (ADR 014's sibling change in
  `adapters/pricing.py`).
- A conformance rate is a permanent artifact of every run, so the README states
  a measurement where it previously carried a caveat.
- The one accommodation the pipeline makes to a non-enforcing transport is
  bounded in writing, tested from the rejection side, and published as a rate.
- The escalation venue is decided while it is still a hypothetical, so the
  choice cannot be rationalised afterwards by whichever route is cheapest to
  reach in the moment.
