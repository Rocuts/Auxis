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

Three names are doing three different jobs in that table and are worth
separating once, here, because the rest of this project's documentation now
uses them in exactly this sense:

- **Protocol** — the **Anthropic Messages protocol**. It is what the three
  adapter classes speak, and it is why one adapter serves every route.
- **Endpoint** — configuration. Live and local, it is the **Vercel AI
  Gateway** (`ai-gateway.vercel.sh`), which accepts that protocol on behalf
  of non-Anthropic models. **Direct Anthropic** (`api.anthropic.com`) and
  **Bedrock** (AWS, designed-only) are the other two selectable routes, and
  neither is funded on this project: no direct-Anthropic key is provisioned,
  and every measured run recorded here went through the gateway.
- **Model** — the ids above, chosen per role.

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

### 8c. The extra-attribute dictionary, adopted once and CLOSED

The third gate run (39/128) moved the failure outward again and made the
remaining gap measurable: of 128 records, **85 differed on nothing but
absent `attrs` keys**, and not one differed on a wrong value. `rate_unit`
and `effective_date` on 51 records each, `imposes_state_sales_tax` on its
46 positive cases, `jurisdiction_name`, `superseded_effective`,
`employer_match`, `threshold`, `unlimited`, `floor_amount`,
`earned_income_addition`. `CANONICAL_CONVENTIONS` simply never named them.

**Operator ruling, under the §8 boundary and consistent with it: the
expected attribute KEY NAMES are adoptable encoding vocabulary.** They pass
the same extractability test `US-FED` passed — `rate_unit` prints in no PDF,
`imposes_state_sales_tax` prints in no PDF — while every value beneath them
is read from the page: the unit from *"All rates are expressed as
percentages"*, the effective date from *"Rates in effect as of January 1,
2026"*, the supersession date from *"taxable years beginning before January
1, 2026"*, the dash convention from the note that explains the dash.

Two conditions attach, and both matter more than the ruling itself:

1. **One pass, then closed.** The dictionary was built COMPLETE — every
   record type, every key — in a single reading of the target schema, and
   `CANONICAL_CONVENTIONS` now declares it closed. There will be no further
   incremental vocabulary rulings. Adopting a vocabulary one gate run at a
   time is how a spec becomes a transcript of the oracle; adopting it once,
   in full, is a specification.
2. **`imposes_state_sales_tax` gets both halves.** The old rule said to
   emit `false` for the five dashed jurisdictions and was silent about the
   other 46 — a rule written for the exception that forgot the norm, which
   is why 46 records were missing a field the schema thought it required.
   The rule now states both: `true` wherever a state rate prints, `false`
   only on the dash.

The boundary is unchanged in every other respect: per-record values remain
forbidden, `src/` never opens the oracle at runtime, and values stay
extract-only. What was adopted is a set of names and the derivation rule
under each — never a value.

### 8d. Output discipline is per role, not per schema-law

The conventions are shared verbatim across all three roles because they are
the part that must NOT differ (ADR 012). The response ENVELOPE is the
opposite: each role returns a different one. While the output-discipline
paragraph lived inside the shared conventions, every role inherited the
*mapper's* — an instruction to put commentary in `"issues"`, a key only the
mapper's schema has and one both other schemas forbid under
`additionalProperties: False`.

For the verifier this was not a tidiness problem. The token `verdicts`
appeared **nowhere** in its prompt: every leaf was named (`record_index`,
`reason`, `confirmed`, `disputed`) and the container never was, while the
wrong key was named twice. Through a gateway that forwards `output_config`
without enforcing it, the prompt is the only channel carrying the envelope —
so the model was told the wrong key and never the right one, and document 05
lost verification three times consecutively, through both bounded retries.
Three consecutive failures is not the per-call coin flip the mapper's
prose-after-JSON was; it is the signature of a systematic mismatch.

This is the mapper's §7 minimization at the altitude that actually
transfers: **stop letting a non-semantic detail the pipeline already holds
cost the whole document.** For the mapper that detail was `source_page` (18
of document 05's 19 records); here it is the envelope's own name. Each role
now states its own envelope; the mapper's text is byte-identical to what it
was, so its prompt is unchanged. The verifier additionally drops `reason`
from its item-level `required` — the one required-but-derivable key it had,
since absent, null and blank already mean the same thing to the parser.

Nothing is adapted at the parse layer and nothing is guessed: a body that
still lacks the envelope is still a hard failure. What changed is that the
failure now names the SHAPE that arrived — keys and type names only, never
document content — so the next occurrence is a measurement rather than the
inference this one required.

### 8e. Reconciliation, not extension — and the spec is frozen

The fourth gate returned **119/128**: four of five documents perfect, `miss`
and `extra` both zero for the first time, 1,614 fields compared and 11
differing. All nine failures sat in document 04 and every one was `actual
<absent>` — a dictionary key not emitted, never a wrong value.

**The cause was a contradiction, not a gap.** The per-type shape bullets and
the §8c dictionary disagreed, and the model followed the older, more specific
text: shapes said *"surtax_threshold: … amount = the threshold"* while the
dictionary said `threshold` is an attribute. The proof that this was a
conflict rather than a capability limit is arithmetic: `surtax_threshold`
scored **4/9**, and the four that passed were document 05's, whose thresholds
sit in a footnote with no `amount` column to divert them. Same key, same
model, same run — the value went to the attr only when the page offered no
alternative slot.

**Ruling: the closed dictionary is the authoritative text; stale bullets
lose.** The entire conventions document was reconciled against it in one
exhaustive sweep — every per-type bullet checked, not only the three that
failed — with contradicting text deleted or rewritten. The dictionary itself
did not change by one key. This is **reconciliation, not extension**: §8c's
"closed" holds, and nothing here reopens it.

The sweep also removed two stale clauses that had survived the vocabulary fix
of §8 — `additional_standard_deduction` and `employment_tax_rate` still said
their `attribute_key` was the printed label *"as printed"*, contradicting the
fixed vocabulary three sections below.

**The mid-gate repair, ratified retroactively.** Enumerating all fifteen
required record keys in the prompt was done mid-gate, under time pressure,
without a ruling. It is ratified here as **§8d-class prompt completion**:
schema-declared requirements the prompt never stated. It invented nothing,
defaulted nothing and adapted nothing — a record still missing a key is still
refused, because defaulting `confidence` would have fabricated the model's own
certainty, the one value in the schema derivable from nowhere else. Recording
it rather than absorbing it is the point: an undisclosed mid-gate change is
indistinguishable from tuning against the oracle.

**The class is now unrepresentable.** Three instances of "the pipeline knew
something it never told the model" is a pattern, not a coincidence, so it is
now a test: `tests/mapping/test_prompt_schema_parity.py` walks each role's
response schema, collects every key named in any `required` list at any
depth, and asserts each appears in that role's prompt text. It runs keyless
and offline in milliseconds.

It found a fourth instance immediately — the mapper's *issue* schema requires
`row_index`, `col_index` and `raw_value`, and the prompt said only "with its
coordinates and a reason". That one never reached a gate. **`required` in a
JSON schema is a claim a non-enforcing gateway does not check; the prompt is
the only channel that carries the contract, and now the two cannot drift.**

**Verifier conventions, same sweep.** Numeric equality is Decimal equality:
`$192.30` mapped as `192.3` and `$1,250.00` as `1250` are correct, and
trailing zeros, dropped currency signs, stripped separators and
percent-to-fraction conversions are formatting, never disputes. Three of the
gate's nine disputes were exactly that, and each cost a human a review row
and found nothing.

The fourth false positive is **kept as a documented limitation, not fixed**:
the verifier reproducibly claims document 01's record 19 holds `257300` when
it holds `257250` — the same misread recorded against this model in the ADR
012 diff review. It is a model-level defect this project cannot correct from
the prompt, and its cost is bounded and visible: one review-queue row, on a
record that scored correct. A false positive that produces one human review
is the system working; the failure mode worth fearing is the false *negative*,
and cross-family verification is what buys protection against it.

**Against that cost, what the verifier bought.** Five of its nine disputes
named exactly the failing `additional_medicare` records, with the right
reason, by independent re-derivation — before any oracle contact. ADR 012's
cross-family mitigation, vindicated empirically rather than argued.

**The spec is FROZEN.** No further conventions passes. If the final run lands
short of 128/128 it ships truthfully, with every failing record named as a
known limitation, and the harness is never touched.

### 8f. The bounded regression revert — SUPERSEDED BY §8g

**Status:** ~~operator ruling, 2026-08-27~~ **SUPERSEDED the same day by
§8g**, on evidence from the pre-run audit. Left standing rather than deleted,
because the premise it got wrong is the point: this section argued for
removing *one clause* on the grounds that doing so restored a measured
configuration, and that was **false**. The reasoning below is preserved as
written; §8g states what replaced it and why.

> **The specific error, named.** §8f's "What was NOT reverted, deliberately"
> defended keeping *"same granularity and typed slots as
> ordinary_income_bracket"* on the grounds that it "earned document 04's
> 9/18 → 18/18 and document 05's continued 19/19". **Document 04 contains no
> `preferential_gain_bracket` records at all** — its 18 are `wage_base`,
> `surtax_threshold`, `employment_tax_rate` and `withholding_allowance` — so
> that bullet cannot have earned any of those nine, and document 05 was
> already 19/19 at gate 4 without the language. The retained text had exactly
> the property §8f condemned in the clause it removed. Credit where it is
> due: a pre-run audit refuted this against the tree, before the run.

**Original scope claim:** one clause deleted from `CANONICAL_CONVENTIONS`.
Nothing else, in any file that reaches a model.

The fifth gate scored **100/128** under the spec frozen at `1961126`. It
repaired all nine of the fourth gate's failures and broke twenty-eight that
had scored perfectly twice. All 28 are one field:
`ordinary_income_bracket.taxpayer_class`, expected `individual`, got null.
The cause is confirmed fix 3 of that run's pre-run audit, which added to the
`preferential_gain_bracket` bullet — directly below the ordinary-income rule —

> `but by filing status ONLY: taxpayer_class is null on this record type.`

**The ruling: remove that sentence and nothing else.** Declared here as
**SPEC FREEZE v2**, in force from the revert commit.

#### Why this is not the sixth conventions pass in disguise

The freeze exists for one reason: to stop the specification being tuned,
run by run, against a scoring harness — at which point it stops being a
specification and becomes a curve fit. That is a real hazard and this ADR
has taken it seriously five times. It is worth stating the case against this
revert before answering it, because from the outside the two look identical:
*a run scored badly, we know which edit caused it, and we are editing it.*

Four properties separate them, and all four have to hold:

1. **No new information enters, from the oracle or anywhere else.** The
   revert is a **deletion**. It adds no rule, no vocabulary, no value, no
   hint. The convention it restores — `taxpayer_class` carried explicitly as
   `individual` on ordinary-income records rather than left null — was
   adopted under the **§8 target-contract boundary ruling**, on the
   extractability test: `individual` is an *encoding* of a fact the page
   states by publishing a filing-status schedule, in the same class as
   `US-FED`, and it was agreed long before this regression existed.
2. **It restores a configuration that was measured, not one that is hoped
   for.** `ordinary_income_bracket` scored **32/32 in the third gate and
   32/32 in the fourth**, with this clause absent from the prompt in both.
   The revert returns that bullet's taxpayer_class semantics to the text that
   produced those two results. This is not a prediction about what the model
   will do; it is the removal of the single documented difference between a
   configuration that scored 32/32 twice and one that scored 4/32 once.
3. **It removes text this project wrote yesterday, not text the corpus
   needed.** The clause is nine hours old and has appeared in exactly one
   gate run. Reverting it does not reach back into any considered ruling.
4. **The risk it guarded never materialised, and the guard was already
   redundant.** This is the decisive one, and it is measured rather than
   argued. Fix 3 existed to stop 12 `preferential_gain_bracket` records from
   taking a `taxpayer_class`. But the field-level bullet, forty lines above
   in the same prompt, already said so **by name**:

   > `taxpayer_class` … is set on ORDINARY_INCOME_BRACKET RECORDS ONLY … on
   > every other record_type — *including preferential_gain_bracket, which
   > does carry a filing_status* — taxpayer_class is null.

   That sentence is **byte-identical across gates 3, 4 and 5**, and
   `preferential_gain_bracket` scored **12/12 in all three**. The protection
   was in place, explicit, named and empirically demonstrated before fix 3
   was written. Fix 3 restated a rule the prompt had already stated — and a
   restatement is not free. It is a change in emphasis, and emphasis is what
   a model generalises. The clause bought nothing measurable and cost 28
   records.

**The distinguishing test, stated so it can be applied again:** a change is
tuning if it adds information the harness supplied. It is a regression revert
if it removes information the project supplied between two measurements.
This one is the second kind. The freeze exists to prevent tuning against the
harness — not to enshrine a documented accident.

#### What was NOT reverted, deliberately

The rest of the reconciliation stands, including the parts of the same bullet
that fix 3 travelled with: `preferential_gain_bracket` keeps *"same
granularity and typed slots as ordinary_income_bracket"* and *"Carries
superseded_effective when its document is superseded"*. Those earned document
04's 9/18 → 18/18 and document 05's continued 19/19. A revert to the fourth
gate's text wholesale would have traded twenty-eight records back for nine —
which is the same mistake in the other direction.

#### Pre-registration for the sixth run

Written before the run, so the disposition cannot be chosen after seeing the
number:

- The sixth gate runs under **exclusive conditions**: nothing else on the
  gateway, the isolation sentinel respected, and **zero edits to any file
  while it is in flight**.
- **Whatever it lands, it ships.** 128/128 closes gate 2b clean. Anything
  short closes it *truthfully*, with every failing record named as a known
  limitation, exactly as the fifth gate's 28 are named now.
- **There is no seventh run, and no further spec text change of any kind** —
  not a clause, not a word, not a reordering. If the sixth run reveals a new
  defect, it is written up as a limitation and shipped as one.
- The blast radius of this revert is **measured, not assumed**: the sixth
  run's `preferential_gain_bracket` score is reported explicitly against the
  12/12 baseline, because that is the risk fix 3 was guarding and the only
  honest way to price the revert.


### 8g. Both bullets to their best-measured text — the revert that supersedes §8f

**Status:** operator ruling, 2026-08-27, on the pre-run audit's evidence.
**SPEC FREEZE v2** is declared at the commit carrying this section.

#### What changed, exactly

Three deletions inside `CANONICAL_CONVENTIONS`, no additions anywhere:

| Bullet | Gate-5 text (100/128) | Now |
|---|---|---|
| `ordinary_income_bracket` | …that column's bounds. **`No extra attrs.`** | …that column's bounds. |
| `preferential_gain_bracket` | "same granularity and typed slots as ordinary_income_bracket — one record per (bracket row x filing-status column) — …, **but by filing status ONLY: taxpayer_class is null on this record type.** Carries superseded_effective…" | "same shape for preferential (e.g. capital gain) rate schedules." |

**Both bullets are now byte-identical to their text at `4739d62`** — the gate
that scored **119/128**, with `ordinary_income_bracket` **32/32** and
`preferential_gain_bracket` **12/12**. That is a property this section can
assert mechanically, and §8f could not.

#### Why this is wider than §8f and *safer* than §8f

§8f removed one clause and left two gate-5 additions in place. The audit
established that both are load-bearing hazards:

1. **The surviving inheritance pointer was an inverse leak.** Gate 5 did not
   only add fix 3; it also strengthened the same bullet from a bare
   cross-reference to *"same granularity and typed slots as
   ordinary_income_bracket"*. `taxpayer_class` **is** one of those typed
   slots. Deleting only fix 3 would have left that pointer unqualified —
   pointing `preferential_gain_bracket` at the one record type that sets
   `taxpayer_class`, with the sentence that scoped it gone. And because
   `taxpayer_class` is a component of the accuracy natural key, a leak there
   does not score as one wrong field: it scores as **miss + extra**, and the
   record is lost whole. Worst case 12/12 → **0/12** — the gate-5 failure
   running backwards.
2. **The surviving in-bullet clause was an unexcluded confound.** Gate 5 also
   appended *"No extra attrs."* to the `ordinary_income_bracket` bullet
   itself, and rewrote the section preamble to frame every bullet as "which
   typed slots it fills". Gate 5's causal story — an *adjacent* clause
   generalised upward — was an inference, never an ablation. A terminal "No
   extra attrs." on the only sentence describing that record type is a
   competing explanation for the same 28 nulls, and it sits *inside* the
   bullet rather than next to it. §8f would have left it in and called the
   result a test of the adjacency hypothesis. **§8g excludes the confound by
   construction instead of arguing about it.**

#### Why all three deletions are information-free

This is the test §8f introduced and the one that matters: *a change is tuning
if it adds information the harness supplied; it is a regression revert if it
removes information the project supplied between two measurements.* All three
deletions pass, and two of them pass twice over — because **every rule
deleted here is still stated, identically, in the closed attribute
dictionary**, which the section's own preamble declares authoritative:

> "the extra-attribute dictionary below is the authoritative list of attr
> keys per record type, and where anything in this section appears to
> disagree with it, the dictionary wins."

| Deleted restatement | Dictionary entry, unchanged since gate 4 |
|---|---|
| "No extra attrs." | `- ordinary_income_bracket: no extra attrs. Everything it states has a typed slot.` |
| "Carries superseded_effective when its document is superseded." | `- preferential_gain_bracket: superseded_effective.` |
| "taxpayer_class is null on this record type" | field-list bullet: "on every other record_type — **including preferential_gain_bracket** — taxpayer_class is null" |

Every one of those targets is byte-identical at gate 4 and at HEAD. **Nothing
was removed except emphasis** — and emphasis is precisely what gate 5 proved a
model generalises. Three restatements of rules the prompt already carried; the
first cost 28 records, and the other two were never measured at all.

#### Document 04's recovery is verified independent of both bullets

The nine records gate 5 repaired are `wage_base` (0/3 → 3/3),
`surtax_threshold` (4/9 → 9/9) and `employment_tax_rate` (3/4 → 4/4). Their
bullets and the dictionary rules governing them are untouched by §8g,
non-adjacent to both edited bullets, and opposite in instruction polarity.
**Document 04 contains no `ordinary_income_bracket` and no
`preferential_gain_bracket` records at all.** Nothing gate 5 earned is given
back.

#### Pre-registered predictions for the sixth run

Written before the run so the result can be scored against them rather than
explained after them — which is the discipline gate 5's arbitration lacked:

| Record type | Gate 4 | Gate 5 | Predicted, gate 6 |
|---|---|---|---|
| `ordinary_income_bracket` | 32/32 | 4/32 | **32/32** |
| `preferential_gain_bracket` | 12/12 | 12/12 | **12/12** |
| `wage_base` | 0/3 | 3/3 | **3/3** |
| `surtax_threshold` | 4/9 | 9/9 | **9/9** |
| `employment_tax_rate` | 3/4 | 4/4 | **4/4** |
| all other types | perfect | perfect | **unchanged** |
| **TOTAL** | 119/128 | 100/128 | **128/128** |

If the sixth run lands short, the *shape* of the shortfall is the finding:
28 back on `ordinary_income_bracket` would mean the true cause was never
either deleted clause; a loss on `preferential_gain_bracket` would mean the
gate-4 bare cross-reference is weaker than three runs suggested.

#### Known limitations this audit surfaced and this ADR does NOT fix

Reported rather than repaired, because repairing them is out of the ruling's
scope and the spec is frozen:

- **`taxpayer_class` has no enforcement below the prompt.** No enum in the
  mapper's JSON schema (`filing_status` has one), no coupling to
  `record_type` in the domain model, no rule in `validation/`, no `CHECK` in
  `migrations/0003_records.sql`. Worse than merely permissive: the column is
  a member of both `records_natural_key` and the `no_overlapping_brackets`
  exclusion constraint, so a wrong value **silently repartitions** the
  integrity chain rather than colliding with anything. The database cannot
  catch this class of error the way it catches bracket overlap.
- **The independent verifier is structurally blind to a conventions defect.**
  `CANONICAL_CONVENTIONS` is concatenated verbatim into the mapper's, the
  verifier's *and* the adjudicator's system prompts. A defect in that shared
  string is not an independent-derivation disagreement — both models read the
  same law. Gate 5 measured it: **1 dispute against 28 broken records**,
  where gate 4's verifier had named 5 of 9 real failures. ADR 012's context
  isolation is real for *values* and absent for *conventions*.
- **One latent site remains, unguarded.** The `tax_year` bullet states a
  general principle and names exactly one of the four record types it
  governs — the same shape as the defect that cost 28 records. Named here so
  the next occurrence is a measurement rather than a surprise.


### 8h. Outcome of the sixth run — both §8g predictions falsified, and the gate closes

**81/128.** Recorded here against the predictions §8g wrote before the run,
which is the entire reason those predictions exist.

| Record type | Gate 4 | Gate 5 | §8g predicted | **Gate 6 actual** |
|---|---|---|---|---|
| `ordinary_income_bracket` | 32/32 | 4/32 | 32/32 | **4/32** |
| `preferential_gain_bracket` | 12/12 | 12/12 | 12/12 | **0/12** |
| `special_gain_rate` | 3/3 | 3/3 | 3/3 | **0/3** |
| `surtax_threshold` | 4/9 | 9/9 | 9/9 | **5/9** |
| `wage_base` | 0/3 | 3/3 | 3/3 | **3/3** |
| `employment_tax_rate` | 3/4 | 4/4 | 4/4 | **4/4** |
| **TOTAL** | 119/128 | 100/128 | **128/128** | **81/128** |

#### Failure 1 — the revert bought nothing on its target

`ordinary_income_bracket` did not move. Both fix 3 and the `No extra attrs.`
clause were deleted, and document 01 scored exactly what it scored with both
in place. **Gate 5's causal story and the pre-run audit's competing story are
both refuted.** The cause of the 28 is unidentified and this ADR does not
supply a third guess; the remaining untested difference from gate 4 is the
rewritten section preamble, and testing it would require a seventh run the
pre-registration forbids.

#### Failure 2 — §8g's own "information-free" test was wrong

This is the finding worth carrying forward, because it invalidates a test this
ADR introduced two sections earlier.

§8g justified deleting *"Carries superseded_effective when its document is
superseded"* on the grounds that the closed dictionary states
`preferential_gain_bracket: superseded_effective` and that entry is
byte-identical at gate 4 and now — therefore the deletion removed a
restatement and no information.

**All 19 of document 05's records then omitted `superseded_effective`.** At
gate 4, the dictionary entry alone was sufficient; at gate 6, same bullet and
same entry, it was not. The test is therefore refuted as stated, and its
corrected form is:

> A deletion is information-free only if the rule's *surviving* statement is
> still read in the same context it was read in when it last worked.
> Byte-identity of the surviving statement is not enough — the reader's
> context is part of the statement.

Which is the same neighbourhood principle §8g invoked against gate 5, applied
to §8g. The pre-run audit said so explicitly ("this is a fourth, never-run
prompt state ... the lesson cuts at this revert too"), that warning was
recorded in §8g, and it was under-weighted in the prediction table of the very
section that recorded it.

#### What held

Document 04 stayed 18/18. §8g argued its gate-5 recovery was independent of
both edited bullets because that document contains no records of either type,
and the run confirmed it. Conformance held for the third run: 128 items
proposed, zero malformed, verifier answering on all five documents with zero
records flagged unverified. Ten calls, $0.0424.

#### Disposition

**Gate 2b CLOSED at 81/128.** No seventh run; no further spec text. The
escalation rule of §3 is re-evaluated one final time and still does not fire:
Trigger A requires a miss attributable to the *model's* semantic judgment, and
all 47 failures are fields the specification asked for without describing well
enough to get. B1 is 1 hard failure retried and recovered — the run's rate is
83.3% call_ok with 100% item_ok, and B2 is 0%. **Escalating the model would
not move a single one of these records**, which is the same reading this ADR
has reached at every gate since §8.

The best-measured configuration is the fourth run's 119/128. It is recorded
beside 81/128 rather than replacing it, because two pre-registered repair
attempts that both made the number worse are evidence about the method — and
suppressing them would make the remaining number worth less, not more.


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
