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

### 6. Outcome of the pre-registered run

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

Escalation is due, by the §5 route, and is an operator action.

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
