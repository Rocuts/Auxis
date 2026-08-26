# ADR 014 — Semantic-layer model selection, and its pre-registered escalation rule

**Status:** accepted · **Date:** 2026-08-26 · **Phase:** 2b

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
- **Envelope residue** — see the carve-out below.

Escalation is **one step**. Going further (to `anthropic/claude-sonnet-5` or
`claude-opus-5`) is an operator decision, not a rule this page grants.

Both runs' tables — accuracy and conformance, before and after — ship in the
README as model-selection evidence. A single table showing the model that
happened to be chosen is an assertion; two tables with a rule written before
either of them is evidence.

### 4. The carve-out, and the peek that produced it

**This rule was written after one look at real output, and says so.** A
single-document smoke test of the instrumentation (fixture 02, dry, nothing
persisted) returned a body that `json.loads` parsed completely and then
rejected with `Extra data`: the model emitted the correct, complete JSON object
followed by a stray markdown-fence remnant (`` `` ``). The semantic content
conformed; the envelope carried transport residue from a gateway that forwards
`output_config` without enforcing it.

Recording that here rather than quietly folding it into a threshold is the
point of pre-registration. **Envelope residue is not a Trigger B failure**: a
body whose JSON value parses completely, with nothing but whitespace or
markdown-fence characters around it, is a transport artifact, not a semantic
one, and no value in it is guessed or dropped.

That carve-out is **not yet implemented**, and the gate cannot produce a
meaningful accuracy number until it is resolved, because today such a response
fails the whole document. The two live options — accommodate the residue at the
transport boundary and count each occurrence as its own measured rate, or treat
it as a hard failure and escalate — are an open fork for the operator. The
second is currently blocked: see below.

### 5. The escalation target is currently unreachable

Probed 2026-08-26 against the configured key:

| Model | Result |
|---|---|
| `zai/glm-5.3-flash` | 200 — the earlier free-tier 429 has lifted |
| `alibaba/qwen-3-235b` | 200 |
| `anthropic/claude-3-haiku` | 200 |
| `anthropic/claude-haiku-4.5` | **403 — "Free tier users do not have access to this model. Upgrade to paid credits."** |
| `anthropic/claude-opus-5`, `claude-sonnet-5`, `openai/gpt-5-mini`, `zai/glm-5.3` | 429, same free-tier message |

`GET /v1/credits` reports a balance of $4.999 with $0.0006 used, so this is not
an empty wallet: the balance is free-tier allowance, and the account is still
flagged free tier for model access. Cheap ids invoke; anything above the tier's
price ceiling does not.

**Consequence, stated plainly: if the pre-registered rule fires, it cannot be
executed on this key.** Unblocking it is a paid top-up, an operator action.
`anthropic/claude-3-haiku` is the only Anthropic-family id currently reachable;
it is a March 2024 model and is recorded here as a *mechanism* fallback — it
would prove the escalation path runs — never as a quality escalation.

## Consequences

- The model choice is now falsifiable: a rule, two thresholds, and two tables.
- Cross-family verification is applied rather than merely configurable, which
  is ADR 012's conformity mitigation actually paid for.
- Cost lines are correct at first print for every role, including a verifier on
  a provider that publishes no cache discount (ADR 014's sibling change in
  `adapters/pricing.py`).
- A conformance rate is a permanent artifact of every run, so the README states
  a measurement where it previously carried a caveat.
