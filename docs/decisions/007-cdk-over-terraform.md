# ADR 007 — AWS CDK v2 (Python) over Terraform / CDKTF

**Status:** accepted · **Date:** 2026-08-26 · **Phase:** 4

## Context

The brief asks for the AWS design as a deliverable, but there is no AWS
account and no budget (anti-goal #5): the infrastructure code must prove
itself **offline**. The evaluation criterion is a validated design, not a
deployment.

## Decision

AWS CDK v2 in Python, validated by a three-stage offline gate on every push:
`cdk synth` with no AWS credentials present (cdk-nag's `AwsSolutionsChecks`
runs as an aspect, so one unsuppressed error fails the synth), `cfn-lint`
over the synthesized template, and the synthesized cloud assembly
(`infra/cdk.out/`) committed as the reviewable artifact.

## Why not Terraform / CDKTF

`terraform plan` requires provider credentials — the very thing this project
does not have — and `terraform validate` runs offline but checks syntax and
internal consistency, not a fully resolved template: it proves far less than
a synth. CDK's `synth` produces the complete CloudFormation document with
zero AWS API calls, *provided the stack never looks anything up* — which is
why anti-goal #4 bans `fromLookup`/`Vpc.fromLookup`/AMI lookups, and why the
stack is environment-agnostic (no account, no region: a lookup is not just
forbidden, it is unrepresentable). CDKTF inherits Terraform's plan-time
credential requirement and adds a second toolchain.

## Also weighed

- **One language across app and IaC** (Python) — the settled CLAUDE.md
  criterion: the same mypy strict + ruff gates that cover `src/` cover
  `infra/`, and the stack file reads as part of the codebase.
- **cdk-nag** gives a rule pack (AwsSolutions) with per-resource,
  individually-justified suppressions recorded both in the stack source and
  in the synthesized NagReport CSV — auditable review evidence, which is the
  Phase 4 point. (Pinned `<3`: cdk-nag 3.0 removed the `NagSuppressions`
  construct-tree helper the written justifications are attached with.)
- **cfn-lint** validates the *artifact*, not the source — the same document
  a deployment would consume.

## Consequences

The design is expressed as one synthesizable stack (API Gateway + WAF →
Lambda → Step Functions Distributed Map → Textract/Bedrock → RDS PostgreSQL
via RDS Proxy, isolated VPC with endpoints and no NAT). The README states
plainly that it synthesizes and validates but was never deployed. The CDK
CLI arrives via `npx` pinned to the v2 line; `aws-cdk-lib` is a `uv`
dependency group (`infra`) that never reaches a runtime bundle.
