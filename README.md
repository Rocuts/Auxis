# Tax Table Ingestion Service

Accepts PDF documents containing tax tables, extracts the tabular data,
normalizes it into a canonical schema, persists it in PostgreSQL, and exposes
it over a REST API. One domain, three deployment targets: **AWS** (designed in
full as a CDK stack that synthesizes and validates offline — never deployed),
**Vercel** (the live URL target), and **docker-compose** (the one-command local
reproduction).

> Phase 5 completes this README (accuracy table, cost analysis, bottleneck
> section, fixture design, C4 diagrams). The section below exists now because
> the Phase 4 adversarial audit produced it, and honesty ships with the
> finding, not with the polish.

## Honest limitations

**The AWS stack synthesizes and validates but was never deployed.** There is
no AWS account and no budget; `cdk synth` (with credentials stripped),
`cfn-lint`, and `cdk-nag` run on every push, and the synthesized assembly is
committed under `infra/cdk.out/` — but no template has ever met a real
control plane. Statements about deploy-time behavior below come from
documentation and an adversarial audit, not from a deployment.

**The Lambda deploy artifact is incomplete by design.** The functions ship
the real `src/` tree, and the handlers (`tax_tables.aws.handlers.*`) are
real, unit-tested code — but the runtime dependency layer (psycopg, pydantic,
anthropic, boto3, mangum) is a deploy-pipeline build step that intentionally
does not exist. The `app_ingest` database role the Lambdas IAM-auth into is
likewise created by a deploy-time migration (`GRANT` on the records/jobs/
review tables), not by the stack.

**Isolated-VPC endpoint inventory** (from the audit's completeness lens: the
VPC has no NAT and no internet path, so every runtime AWS API call must
traverse a VPC endpoint — a missing one is a deploy-time failure that
synthesis cannot catch). Every call enumerated for the five app Lambdas, the
hosted rotation Lambda, RDS Proxy, Step Functions, and the platform-side log
deliverers resolves to one of the seven endpoints (S3 gateway; interface:
Secrets Manager, Textract, Bedrock runtime, CloudWatch Logs, Step Functions,
X-Ray) or to a path that never leaves the local host (`rds-db:connect` token
signing is local; Lambda credentials arrive via the runtime, no STS call).
Two documented gaps, deliberate:

| Call | Status | Why it is acceptable — and when it stops being |
|---|---|---|
| `bedrock:GetInferenceProfile` / profile-routed invocation | no endpoint, not granted | The stack pins foundation-model IDs. Adopting cross-region inference profiles would require both the IAM grant on the profile ARN and routing that this VPC cannot express today. |
| `cloudwatch:PutMetricData` | no endpoint | No code emits custom metrics (Powertools metrics unused). Enabling them requires the `monitoring` interface endpoint first. |

**The accuracy gate is credential-blocked.** The 128/128 end-to-end accuracy
run (`make accuracy`) executes the real mapper + independent verifier against
the Anthropic API and runs the moment a funded key lands in `.env`; until
then the pipeline's accuracy claims are the design target, not a measured
result. Everything else in `make check` (377 tests) runs keyless.
