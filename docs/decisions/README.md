# Architecture Decision Records

One page each, written when the decision became real. Where a decision is made
but its implementation waits on an open gate, the ADR says so in its status
line rather than implying code that does not exist.

| # | Decision | Phase | Status |
|---|---|---|---|
| 001 | [PostgreSQL as the database engine](001-postgresql-engine.md) | 1 | accepted, implemented |
| 002 | [Aurora DSQL rejected](002-aurora-dsql-rejected.md) — no `EXCLUDE` clause, no range types | 1 | rejection |
| 003 | [RDS Data API rejected](003-rds-data-api-rejected.md) — Aurora-only, and a second untestable repository | 1 | rejection |
| 004 | [Aurora Serverless v2 rejected](004-aurora-serverless-v2-rejected.md) — RDS Proxy prevents auto-pause; migration threshold recorded | 1 | rejection |
| 005 | [Polymorphic fact table over per-type tables](005-polymorphic-fact-table.md) | 1 | accepted, implemented |
| 006 | [Hybrid extraction router](006-hybrid-extraction-router.md) — deterministic first, OCR only when unavoidable | 2 | accepted, implemented |
| 007 | [CDK over Terraform](007-cdk-over-terraform.md) — offline synth | 4 | accepted, implemented |
| 008 | [Vercel as the live demo target](008-vercel-as-the-live-target.md) | 3.5 | accepted; **implementation pending** |
| 009 | [Cron-sweep `JobRunner` on request-scoped compute](009-cron-sweep-jobrunner.md) — Queues unavailable on this account | 3.5 | accepted; sweep built, cron pending |
| 010 | [Vision-OCR as the Vercel extractor for scanned input](010-vision-ocr-vercel-extractor.md) | 3.5 | accepted; **implementation pending** |
| 011 | [Blob-in-Postgres (`bytea`) vs Vercel Blob](011-blob-in-postgres-vs-vercel-blob.md) | 3.5 | accepted, implemented |
| 012 | [Runtime multi-agent semantic layer: mapper + verifier + adjudicator](012-runtime-multi-agent-semantic-layer.md) | 2 | accepted, implemented |
| 013 | [Tracing without the X-Ray SDK, directly or transitively](013-tracing-without-the-xray-sdk.md) | 5 | accepted, enforced by test |
| 014 | [Semantic-layer model selection, and its pre-registered escalation rule](014-semantic-layer-model-selection.md) | 2b | accepted, rule pre-registered |
| — | [Orchestration alignment with Anthropic's published criteria](adr-orchestration-alignment.md) | all | reference |

## The rejections are the load-bearing ones

Three of the thirteen are rejections, and each records **the threshold at which
it would flip** rather than only the reason it was made:

- **Aurora DSQL** would become correct the day it gains range types and
  exclusion constraints — its operational story is genuinely better, and
  persistence is behind a port, so the change would be one adapter and one set
  of migrations.
- **The RDS Data API** would become worth re-evaluating if the AWS target ever
  became something actually deployed and integration-tested, since the
  objection is almost entirely about an untestable divergent code path.
- **Aurora Serverless v2** needs three conditions together, and this
  workload's shape is the honest problem: the fan-out that would justify
  serverless scaling is the same fan-out that requires RDS Proxy, and RDS
  Proxy prevents auto-pause outright.
