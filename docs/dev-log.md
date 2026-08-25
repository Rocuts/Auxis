# Development log

Chronological record of what was tried, what failed, and what was chosen — as
required by the brief ("documentation of development steps and tool choices").
This project is built with heavy, deliberate AI assistance (Claude Code); for an
AI Engineer role that is signal, not something to hide. Orchestration choices are
logged here alongside engineering ones.

## 2026-08-25 — Project setup

- **Fixtures committed first.** The brief supplied no documents, so the five
  input PDFs and `ground_truth.json` are self-authored, produced by the committed
  generator scripts (`fixtures/gen_fixtures.py`, `gen_groundtruth.py`,
  `scanify.py`). Each document is designed to break a different naive extraction
  assumption; see CLAUDE.md "The input documents".
- **Environment verified before writing code.** Docker 29.7.2 (local Postgres for
  tests), uv 0.10.10, Node 25, Vercel CLI 59.5.0 linked to project `auxis` with
  the Neon Marketplace integration attached.
- **Finding: Vercel Queues is not available on this account's scope.** The
  dashboard shows no Queues product. Decision: the Vercel `JobRunner` adapter
  will be the cron-sweep fallback anticipated in CLAUDE.md — a jobs-table sweep
  driven by a `vercel.json` cron at minute granularity. The latency trade-off
  will be recorded in the JobRunner ADR at Phase 3.5.
- **Finding: the Neon integration creates its env vars as Sensitive**, so
  `vercel env pull` returns `[SENSITIVE]` placeholders instead of values. Runtime
  functions receive real values; local runs against Neon (the Phase 1 gate) need
  the connection string copied from the Neon console into `.env`.
- **Tool discipline encoded in the repo.** `.claude/settings.json` allowlists the
  dev toolchain (uv, pytest, ruff, mypy, docker, psql, git, curl, node, make),
  forces a prompt on every `vercel` command, and hard-denies
  `vercel deploy --prod` / `promote` / `rollback` and `cdk deploy` / `bootstrap` —
  anti-goals #5 and #9 enforced at the harness level, not just documented.
- **Orchestration plan.** Sequential single-agent work by default. Multi-agent
  fan-out is reserved for the three phases where it genuinely helps: Phase 2
  (one agent per fixture PDF, adversarial verification of extracted records),
  Phase 4 (audit of the synthesized CloudFormation template), Phase 5 (final
  review against the evaluation criteria).

## 2026-08-25 — Phase 0: scaffold

- `uv` project, Python 3.12, src layout (`src/tax_tables/`), hatchling build so
  the package installs editable and imports cleanly from tests.
- ruff (lint + format) with a broadened rule set, mypy `strict = true`, pytest.
  One smoke test so `make check` exercises the whole toolchain on the empty
  project rather than trivially passing with zero tests collected.
- `make check` = lint + typecheck + test; the same entrypoint runs locally and in
  CI (GitHub Actions, `astral-sh/setup-uv` with caching) so the Phase 0 gate is
  one command in both places.
- `.env.example` documents every required variable per anti-goal #10; `.env` is
  gitignored and never enters the repo.
