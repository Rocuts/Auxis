# ADR 012 — Runtime multi-agent semantic layer: mapper + independent verifier + adjudicator

**Status:** accepted · **Date:** 2026-08-25 · **Phase:** 2

## Context

Phase 2b shipped a single-model semantic layer: one `SchemaMapper` call maps an
extracted grid to canonical records. Its output is tax data; a plausible-but-wrong
value that passes triage persists as authoritative. The mapper cannot audit
itself — a model reviewing its own transcript inherits its own reading of the
document. Separately, the review queue is append-only: every FLAG and mapping
issue waits for a human, including items the document itself can settle.

Anthropic's published bar for adding agents is that complexity is added "*only*
when it demonstrably improves outcomes" and that multi-agent systems "excel at
valuable tasks that involve heavy parallelization, information that exceeds
single context windows, and interfacing with numerous complex tools" — and are
economically viable only "where the value of the task is high enough to pay for
the increased performance" ([Building Effective AI
Agents](https://www.anthropic.com/engineering/building-effective-agents);
[multi-agent research
system](https://www.anthropic.com/engineering/multi-agent-research-system)).

## Decision

The semantic layer becomes a **mapper + independent verifier pair**; the review
queue gains an **adjudicator**. Three model roles, each a bounded, single-pass
port with an Anthropic-API adapter. Nothing else in the pipeline becomes an agent.

- **`RecordVerifier`** receives the mapped records plus the *same* serialized
  grid/prose context the mapper saw — in a fresh context, under a skeptic
  prompt, never the mapper's reasoning. It confirms or disputes each record's
  values and provenance citations; a dispute persists the record as
  `needs_review` and lands in the review queue with its reason. It never
  corrects a value (anti-goal #8).
- **`Adjudicator`** makes a single pass over open review-queue items: one item,
  the full extracted evidence, a proposed resolution with citations and a
  confidence. At/above a threshold it auto-resolves with an audit trail
  (resolution, citations, `resolved_by`, `resolved_at`); below, the item stays
  human with the proposal stored. Citations are validated against the extracted
  document; dangling citations never auto-resolve.

## Justification per role, against the published criteria

- **Parallel tasks.** Verification is per-document and independent of other
  documents — it rides the existing per-fixture fan-out ("breadth-first queries
  that involve pursuing multiple independent directions simultaneously").
  Adjudication is per-item and independent between items.
- **Specialization.** The mapper's objective is production; the verifier's is
  refutation; the adjudicator's is disposition of one flagged item. These are
  different prompts with different success criteria — exactly the "second
  opinion" pattern Anthropic recommends: "a fresh model try to refute the
  result, so the agent doing the work isn't the one grading it"
  ([Claude Code best practices](https://code.claude.com/docs/en/best-practices)).
- **Context isolation.** The verifier's value comes precisely from *not* sharing
  the mapper's context: agreement between two independent derivations is
  evidence; a model agreeing with its own transcript is an echo. Subagents
  "operating in parallel with their own context windows" is the published
  mechanism ([multi-agent research
  system](https://www.anthropic.com/engineering/multi-agent-research-system);
  [context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)).
- **Value vs. token economics.** Multi-agent systems cost ~15× chat-level
  tokens; that is spent here only on the semantic layer of tax data, where a
  silent error is the worst failure mode the product defines (anti-goal #8),
  and it is measured: per-document cost is itemized mapper / verifier /
  adjudicator, so the README can state what verification costs.
- **Conformity risk, mitigated by config.** Anthropic's August 2026 finding —
  "when one agent makes a bad decision, it is likely that many agents will make
  that same bad decision" ([Patterns and problems in multiagent
  systems](https://www.anthropic.com/research/multiagent-systems)) — cuts
  against same-model double-checking. The verifier therefore accepts its own
  model config (`RECORD_VERIFIER_*`), permitting a cheaper or different-family
  model than the mapper.

## Why extraction and routing stay deterministic

"Finding the simplest solution possible" is the first published criterion, and
for structural extraction the simplest working solution is not a model: the
router is a two-branch rule over measurable page properties, and `pdfplumber`
reads actual PDF table structure with higher fidelity than any model inferring
it from pixels, at $0, reproducibly. An agent there adds nondeterminism,
latency, and per-document cost with no demonstrable outcome improvement — the
exact condition under which Anthropic says not to add it. Model judgment is
confined to the one layer whose task *is* judgment.

## Bounds

Single pass per role; no loops, no agent-to-agent negotiation, no
self-correction cycles. Each role is invoked like a tool call — defined input,
schema-enforced output — the shape Anthropic identifies as the working regime
for multi-agent systems, versus long-lived peers, which it identifies as the
failing one. Disagreements route to the review queue, not to a conversation.

## Consequences

Two more paid calls per document ceiling (verifier; adjudicator only when the
queue is non-empty), itemized in every report. The accuracy gate is unchanged —
128/128 judged on the mapper's raw output — with a new per-document
disagreement column making verifier friction visible instead of averaged away.
Live behavior stays behind the credential smoke test; unit tests run on
hand-built fixtures with fake clients, like the mapper's.
