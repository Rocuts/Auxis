# Architecture Decision Records

One page each, written when the decision becomes real. Planned set (from
CLAUDE.md), in rough phase order:

| # | Decision | Phase |
|---|---|---|
| 001 | PostgreSQL as the database engine | 1 |
| 002 | Aurora DSQL rejected (no exclusion constraints) | 1 |
| 003 | RDS Data API rejected (Aurora-only, breaks portability) | 1 |
| 004 | Aurora Serverless v2 rejected (resume latency vs. cost; migration threshold) | 1 |
| 005 | Polymorphic fact table over per-type tables | 1 |
| 006 | Hybrid extraction router (deterministic first, OCR fallback) | 2 |
| 007 | CDK over Terraform (offline synth) | 4 |
| 008 | Vercel as the live demo target | 3.5 |
| 009 | Cron-sweep JobRunner on request-scoped compute (Queues unavailable on this account) | 3.5 |
| 010 | Vision-OCR as the Vercel extractor for scanned input | 3.5 |
| 011 | Blob-in-Postgres (bytea) vs Vercel Blob | 3.5 |
| 012 | [Runtime multi-agent semantic layer: mapper + verifier + adjudicator](012-runtime-multi-agent-semantic-layer.md) | 2 |
| — | [Orchestration alignment with Anthropic's published criteria](adr-orchestration-alignment.md) | all |
