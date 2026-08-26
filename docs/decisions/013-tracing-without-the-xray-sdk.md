# ADR 013 — Tracing without the X-Ray SDK, directly or transitively

**Status:** accepted · **Date:** 2026-08-26 · **Phase:** 5

## Context

Anti-goal #6 forbids the AWS X-Ray SDK (maintenance mode, February 2026) and,
as originally written, recommended "Powertools Tracer or OpenTelemetry" as the
replacement. The first half of that recommendation contradicts the
prohibition, and the contradiction had already reached the CDK stack docstring,
which claimed tracing was "Powertools Tracer … the SDK never appears in the
bundle." Both halves of that sentence cannot be true.

## Decision

**No `aws-xray-sdk`, direct or transitive; tracing on the AWS target is Lambda
active tracing, a platform setting that ships no library.** `aws-lambda-powertools`'s
published metadata (v3.34.0) gates `aws-xray-sdk<3.0.0,>=2.8.0` on
`extra == "tracer" or extra == "all"`, while its base distribution requires only
`jmespath` and `typing-extensions` — so Powertools remains the CLAUDE.md Lambda
toolkit for Logger, Idempotency, and batch partial failure, and only its Tracer
extra is out of bounds. Nothing is lost by declining Tracer, because AWS
documents that with `Active` tracing "Lambda automatically creates trace
segments for function invocations and sends them to X-Ray" with no library in
the deployment package; an SDK is needed only to *extend* the invocation
subsegment with custom spans, and if that is ever wanted here the answer is
OpenTelemetry (ADOT), never Powertools Tracer. Today `uv.lock` contains neither
`aws-xray-sdk` nor `aws-lambda-powertools`, because the Lambda dependency layer
is a deploy-pipeline build step that intentionally does not exist (README,
honest limitations) — so this ADR is a constraint on what that layer may
contain when it is built, and `tests/test_tracing_policy.py` enforces it
against `pyproject.toml`, `uv.lock`, and every import in `src/`, so a lockfile
grep and this page can never drift apart.

## What stays, and why

The stack keeps `tracing=lambda_.Tracing.ACTIVE`, the `xray:PutTraceSegments` /
`xray:PutTelemetryRecords` grants CDK attaches with it, and the X-Ray interface
endpoint. AWS states the permission requirement on the **function's execution
role** — "Lambda needs the following permissions to send trace data to X-Ray.
Add them to your function's execution role" — which is evidence that delivery
is credentialed from, and originates in, the execution environment rather than
from the service side. The VPC has no NAT and no internet path, so the endpoint
is what that delivery traverses.

This also disposes of two findings parked from the Phase 4 audit ("the X-Ray
endpoint and `xray:Put*` grants have no caller"): the caller is the Lambda
runtime, not repository code. Whether the endpoint is *strictly* required for
active tracing from an isolated subnet is a deploy-time verification item, held
to the same standard as everything else this stack has never deployed.

## Consequences

Anti-goal #6 is amended to bind the dependency graph rather than only imports,
and to stop recommending the wrapper of the thing it forbids. The stack
docstring now states the truth: tracing is the platform's, the `POWERTOOLS_*`
variables configure Logger for the dependency layer a deploy pipeline would
add, and Tracer is excluded by name.
