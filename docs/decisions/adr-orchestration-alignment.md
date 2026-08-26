# ADR — Orchestration alignment with Anthropic's published criteria

**Status:** accepted · **Date:** 2026-08-25 · **Scope:** how this project is
*built* (agent orchestration in Claude Code) and how it *runs* (ADR 012)

Criteria cited: simplest-first ("finding the simplest solution possible…
adding complexity *only* when it demonstrably improves outcomes" — [Building
Effective AI Agents](https://www.anthropic.com/engineering/building-effective-agents),
Dec 2024, principles current though its tooling note points to newer posts);
the multi-agent conditions ("heavy parallelization, information that exceeds
single context windows, and interfacing with numerous complex tools", tasks
"where the value of the task is high enough to pay for the increased
performance", and the token economics: agents ≈4×, multi-agent ≈15× chat
tokens; token usage explains 80% of variance in a three-factor model — [multi-agent
research system](https://www.anthropic.com/engineering/multi-agent-research-system),
Jun 2025); explicit guardrails and observability (stopping conditions,
checkpoints for human feedback, sandboxing, tracing, evidence over assertion —
ibid. and [Claude Code best practices](https://code.claude.com/docs/en/best-practices)).

## Choice → criterion

| Orchestration choice | Published criterion it satisfies |
|---|---|
| **Build: sequential single-agent default** in all phases but three | Simplest-first. Most phases are dependency chains (DDL → adapter → tests); no parallelism exists to buy. |
| **Build: the three fan-outs** — Phase 2 (one agent per fixture + adversarial verify), Phase 4 (CFN template audit per resource type), Phase 5 (review per evaluation criterion) | The multi-agent conditions: each is genuinely parallel (independent fixtures / resources / criteria), high-value (the gate results *are* the deliverable), and collectively larger than one context. Subagents return summaries, preserving the lead's context (context isolation). |
| **Build: adversarial find-then-refute reviews** (2a, 2b: finders propose, refute-by-default verifiers must independently confirm) | The "second opinion" gate: "a fresh model try to refute the result, so the agent doing the work isn't the one grading it." Refute-by-default is our answer to Anthropic's warning that reviewers prompted to find gaps over-report: only confirmed findings get fixed. |
| **Build: phase gates with user sign-off; never start the next phase unprompted** | Checkpoints for human feedback; explicit stopping conditions; "human evaluation catches what automation misses." |
| **Build: `/goal` bounds** — measurable end state + no-weakening constraint (nothing under `tests/accuracy/` or `fixtures/` changes) + turn bound | Stopping conditions "to maintain control"; evidence over assertion (the end state is a command's output, not a claim). |
| **Build: guardrails in committed `.claude/settings.json`** — toolchain allowlist, prompt on every `vercel`, hard-deny `deploy --prod`/`promote`/`cdk deploy` | Published permissions guidance: allowlists plus restrictions for unattended runs; anti-goals #5/#9 enforced structurally, not procedurally. |
| **Build: dev-log records orchestration choices; every fan-out's cost and outcome named** | Tracing/observability: "full production tracing let us diagnose why agents failed." |
| **Build: best-practices validation agent before load-bearing decisions** | Context isolation (research burns tokens in its own window, returns a summary) + specialization; it exists because training-data knowledge lags. |
| **Runtime: deterministic router and extraction** | Simplest-first, applied at runtime: a two-branch rule and `pdfplumber` beat a model on structure at $0; adding an agent there demonstrably improves nothing (ADR 012). |
| **Runtime: mapper + verifier + adjudicator, single-pass, tool-shaped** | The multi-agent conditions confined to the one judgment layer; bounded per the Aug 2026 finding that agents cooperate well "insofar as they are able to treat other agents as tool invocations" ([Patterns and problems in multiagent systems](https://www.anthropic.com/research/multiagent-systems)). Per-role cost itemization keeps the ≈15× economics measured, not assumed. |

## Deliberate deviations

**1. All-Opus subagents, not the Opus-lead/Sonnet-workers reference mix.**
Anthropic's research system used "Claude Opus 4 as the lead agent and Claude
Sonnet 4 subagents" (outperforming single-agent Opus 4 by 90.2%); it publishes
*no rationale* for that allocation, so the reasoning here is ours, not
Anthropic's. This project's lead is Fable 5 — a tier above Opus — so
Opus-tier workers *preserve* the published lead>worker capability gradient
rather than inverting it. And the worker economics that motivate cheaper
subagents at research scale (thousands of parallel tool calls) do not bind on
a five-fixture corpus, while the cost of a missed defect in an evaluated
take-home is the deliverable itself. Where cost-tiering does published-criteria
work is at runtime: the verifier's config permits a cheaper or different-family
model, which doubles as the mitigation for Anthropic's conformity-risk finding
(correlated errors across same-model agents).

**2. Fanning out coding work at all.** Anthropic states plainly: "most coding
tasks involve fewer truly parallelizable tasks than research" — and its
C-compiler experiment shows the failure ("every agent would hit the same bug…
and then overwrite each other's changes"). We fan out coding anyway, but only
*after* decomposition has removed the interdependence: probe-first design
produces per-file specs, agents build disjoint files with defined
inputs/outputs (the tool-invocation shape), and the lead does all integration.
The 2a build (four agents, disjoint modules, probe data as spec) is the
pattern; a fan-out whose tasks still share files or decisions is not run —
that work stays sequential under deviation-free simplest-first.
