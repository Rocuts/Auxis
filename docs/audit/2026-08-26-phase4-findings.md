# Phase 4 adversarial audit — the complete finding ledger

Generated from the audit run's own agent output (23 agents: six
resource-type auditors, three directed lenses, then refute-by-default
verification). **75 raw findings.** This file is the full list; the
dev-log and README summarize it.

Dispositions:

- **CONFIRMED · fixed** — the 14 findings the audit verified (0 refuted)
  plus the duplicate reports other agents filed for the same defects: 27
  rows, eight distinct defects, each foreclosed test-first. Independent
  rediscovery is why several defects carry four or five rows.
- **PROMOTED · verified & fixed / documented** — findings the audit named
  but did not verify, promoted afterwards by a title-level sweep against
  three criteria (data loss, money paths, auth) and then verified against
  primary sources — AWS documentation, or decompiled `aws-cdk-lib` —
  rather than accepted on the finder's word.
- **REFUTED on verification** — promoted, checked, and found wrong.
- **PARKED · named, unverified by design.** These were reported by a
  finder agent and never verified. They are recorded here rather than
  discarded, and are not claims about the stack: a parked row may be
  right, wrong, or already obsolete. Nothing in the README or the
  dev-log rests on one.


## CONFIRMED · fixed (Phase 4 audit)

| # | Severity | Title | Disposition |
|---|---|---|---|
| 00 | critical | Every Lambda Handler points at a module that does not exist in the deployed asset | deploy artifact |
| 01 | critical | No dependency layer exists, contradicting the source comment that claims one | deploy artifact |
| 09 | critical | WAF CommonRuleSet blocks every PDF upload: SizeRestrictions_BODY blocks bodies over 8 KB, in Block mode, in front of a 10 MB upload endpoint | WAF body-size block |
| 10 | critical | RestApi declares no BinaryMediaTypes, so PDF bytes reach the ingest Lambda as mangled UTF-8 text | binary media types |
| 11 | critical | State machine has no Catch on any state and no tolerated-failure setting: one bad document aborts the whole batch and no job is ever marked failed | batch abort |
| 17 | critical | VPC flow-log delivery grant is absent from the access-log bucket policy — the flow log either fails to create or silently overwrites the whole bucket policy | flow-log delivery |
| 21 | critical | Every Lambda authenticates to Postgres as the RDS master user, including the public internet-facing API function | master-user auth |
| 28 | critical | RDS Proxy listens on 5432 for PostgreSQL, but the only ingress rule to ProxySg is tcp/5433 — no client can reach the proxy | proxy port |
| 35 | critical | RDS Proxy for PostgreSQL always listens on 5432 — the stack wires every client to 5433, so no Lambda can reach the database | proxy port |
| 36 | critical | Lambda code artifact is not runnable: no dependency layer exists despite the inline claim, and the handler package is absent from the asset | deploy artifact |
| 43 | critical | RDS Proxy listens on 5432 for PostgreSQL, but the only Lambda→Proxy ingress is tcp/5433 and DB_PORT=5433 is injected into every function | proxy port |
| 44 | critical | WAF CommonRuleSet blocks the upload path: SizeRestrictions_BODY blocks any request body over 8 KB | WAF body-size block |
| 45 | critical | API Gateway has no BinaryMediaTypes, so PDF bodies are UTF-8 mangled before the handler sees them | binary media types |
| 56 | critical | Flow-log destination bucket has no log-delivery grant: the deploy either fails on VpcFlowLog, or AWS overwrites the bucket policy and silently deletes the stack's own TLS-enforcement and server-access-log grants | flow-log delivery |
| 57 | critical | No Lambda can reach the database: RDS Proxy for PostgreSQL listens only on 5432, but ProxySg admits only tcp/5433 from LambdaSg and every function's DB_PORT is 5433 | proxy port |
| 63 | critical | Lambda bundle contains neither the handlers nor any dependency, and the promised "dependencies layer" is not in the template | deploy artifact |
| 29 | major | The hosted rotation Lambda cannot reach the database: DbSg admits ProxySg only, so 30-day secret rotation can never complete | rotation SG |
| 30 | major | Every application Lambda authenticates to the database as the RDS master user, which on RDS PostgreSQL is an rds_superuser member | master-user auth |
| 37 | major | Secret rotation can never reach the database — DbSg admits only ProxySg, while the hosted rotation function runs in LambdaSg | rotation SG |
| 40 | major | Every Lambda authenticates to Postgres as the RDS master user, including the internet-reachable API function | master-user auth |
| 47 | major | Flow-log delivery grant is missing from the AccessLogs bucket policy; the first deploy either fails or silently overwrites enforce-SSL and the server-access-log grant | flow-log delivery |
| 48 | major | Secret rotation can never reach the database: the hosted rotation Lambda uses LambdaSg, but DbSg admits only ProxySg | rotation SG |
| 52 | major | No Lambda dependencies ship: Code.from_asset("src") with zero layers and no bundling step, contradicting the stack's "dependencies layer at deploy time" claim | deploy artifact |
| 58 | major | Automatic secret rotation can never succeed: the hosted rotation Lambda runs in LambdaSg, and DbSg has no ingress from LambdaSg | rotation SG |
| 64 | major | Creating the VPC flow log overwrites the access-logs bucket policy, silently deleting the TLS-only Deny and the S3 server-access-log grant | flow-log delivery |
| 65 | major | The hosted secret-rotation Lambda cannot reach the database; 30-day rotation fails permanently and silently | rotation SG |
| 68 | major | Distributed Map has no tolerated-failure budget: one bad document aborts the whole batch and stops in-flight siblings | batch abort |

## PROMOTED · verified & fixed (post-audit sweep)

| # | Severity | Title | Disposition |
|---|---|---|---|
| 03 | major | MaxConcurrency=8 bounds one Map Run, not the system; with no reserved concurrency the public read path can starve the pipeline and the pipeline can exhaust RDS Proxy | reserved concurrency |
| 04 | major | Retry blocks omit Lambda.TooManyRequestsException and ToleratedFailureCount is unset, so one throttled document aborts the batch mid-persist | throttle retry |
| 05 | major | The AwsSolutions-IAM4 suppression's 'no privilege reduction' premise is factually false for both managed policies it excuses | IAM4 justification |
| 12 | major | Task retries omit Lambda.TooManyRequestsException, so a throttle — the expected failure at MaxConcurrency 8 — dead-ends a document | throttle retry |
| 13 | major | Distributed Map has no ResultWriter and no result trimming: child outputs aggregate into the parent output against the 256 KiB limit | Map Run export |
| 23 | major | The stack-level IAM5 suppression has no `appliesTo`: written scope is three wildcard classes, machine scope is every IAM5 finding in the stack, permanently | IAM5 suppression scope |
| 24 | major | IAM4 justification is factually wrong: AmazonAPIGatewayPushToCloudWatchLogs is not log-write only — it grants account-wide log READ | IAM4 justification |
| 25 | major | No principal in the template can record a pipeline failure, so GET /jobs/{id} reports "running" forever for any failure before PersistStep | job failure path |
| 39 | major | Pipeline has no failure path: unretried task errors, ToleratedFailure defaulting to 0, and no branch that ever marks a job failed | job failure path |
| 49 | major | State machine has no failure path: default tolerated-failure 0 aborts the whole fan-out, no Catch writes a terminal job status, and the named bottleneck error class is not retried | job failure path |
| 50 | major | Pipeline steps can only pass data through the 256 KB Step Functions payload, and Extract has read-only S3 so offloading is not grantable | Map Run export |
| 51 | major | IAM4 justification is factually wrong for AmazonAPIGatewayPushToCloudWatchLogs — it grants account-wide log read, not "log-write only" | IAM4 justification |
| 59 | major | Every VPC endpoint is synthesized with the default full-access policy; the unrestricted S3 gateway endpoint is an unauthenticated data-egress path that contradicts the stack's "no path to the internet exists" claim | VPC endpoint policies |
| 66 | major | `ProxySg`'s `allow_all_outbound=False` is not honored in the synthesized template — the RDS Proxy gets EC2's default allow-all egress | proxy egress |
| 31 | minor | ConnectionPoolConfigurationInfo is empty — the proxy's pool, the design's stated fan-out bottleneck mitigation, is entirely unconfigured | proxy connection pool |
| 41 | minor | Distributed Map has no ResultWriter — item list and aggregated results both ride the 256 KiB state payload | Map Run export |
| 53 | minor | MaxConcurrency: 8 bounds one execution, not the fleet — the documented "bottleneck knob" does not bound Bedrock TPS or proxy connections | reserved concurrency |
| 55 | minor | "The X-Ray SDK never appears in the bundle" is false: Powertools Tracer's runtime dependency is aws-xray-sdk | tracing claim |
| 70 | minor | Distributed Map is fed by ItemsPath with no ItemReader or ResultWriter, so the fan-out is bounded by the 256 KiB state payload quota | Map Run export |
| 72 | minor | The anti-goal #6 claim is factually wrong: Powertools Tracer is a wrapper over the AWS X-Ray SDK, so choosing it puts the SDK in the bundle | tracing claim |
| 73 | minor | The stack-level AwsSolutions-IAM5 suppression is unscoped, so no future wildcard grant in this stack can ever be reported | IAM5 suppression scope |

## PROMOTED · verified & documented

| # | Severity | Title | Disposition |
|---|---|---|---|
| 14 | major | The documented 10 MB upload cap is unreachable through API Gateway → Lambda proxy; the real ceiling is ~4.4 MB | upload ceiling (per target) |

## REFUTED on verification

| # | Severity | Title | Disposition |
|---|---|---|---|
| 69 | major | Only `textract:AnalyzeDocument` is granted, and the synchronous API is capped at one page for PDFs with no async path available | the adapter renders each page to PNG; no PDF reaches Textract's single-page sync path |
| 42 | minor | The cdk-nag <3 pin freezes the rule pack on a line with no releases since v3 shipped, and neither pyproject nor ADR 007 records that | the pin IS recorded (pyproject + ADR 007) |
| 62 | minor | The EC23 suppression's written justification names only the endpoint security groups, but apply_to_children stamps it onto 22 VPC-subtree resources including subnets, route tables, the flow log and the default-SG custom resource | effective scope today is exactly the six endpoint SGs the reason names |

## PARKED · named, unverified by design

| # | Severity | Title | Disposition |
|---|---|---|---|
| 02 | major | Powertools is configured on every function but is not a project dependency, and the tracing justification is factually self-defeating | — |
| 18 | major | Documents bucket policy imposes no access boundary, so the stack's "no path to the internet" isolation claim does not cover the tax documents themselves | — |
| 22 | major | Bedrock grant omits the inference-profile ARN, so the exact scenario its suppression justification cites is the one it denies | — |
| 26 | major | AWS::ApiGateway::Account + ApiCloudWatchRole silently overwrite region-wide shared state and leave it pointing at a deleted role on teardown | — |
| 38 | major | Bedrock grant omits the inference-profile resource that its own cdk-nag justification names as the reason for the region wildcard | — |
| 46 | major | Bedrock policy omits the inference-profile ARN, so InvokeModel is denied in exactly the calling mode the suppression's own justification describes | — |
| 60 | major | Environment-agnostic Fn::GetAZs subnet placement can put the RDS Proxy in an AZ where AWS documents it is unsupported, failing the deploy with no possible mitigation under the no-lookup rule | — |
| 67 | major | Bedrock grant cannot invoke a cross-region inference profile — the exact mechanism the suppression's justification cites | — |
| 06 | minor | The custom-resource handler's logs escape the stack's retention policy entirely | — |
| 07 | minor | Audit-log retention contradicts the data-protection posture the same stack sets for the data | — |
| 08 | minor | IngestApi timeout is at the API Gateway integration cap, not under it as the comment claims | — |
| 15 | minor | State machine sets no TimeoutSeconds; a stalled execution can hold Map capacity for up to a year | — |
| 16 | minor | Ingest Lambda timeout equals the API Gateway integration timeout it claims to sit under | — |
| 19 | minor | Tax-document store is encrypted with SSE-S3 only, and the AwsSolutions pack contains no rule that would flag it | — |
| 20 | minor | Audit trail for the tax documents expires at 90 days while the documents themselves are retained indefinitely | — |
| 27 | minor | One of the nine IAM roles is invisible to cdk-nag, so "48 compliant / 47 suppressed / 0 non-compliant" is not a coverage statement | — |
| 32 | minor | The RDS Proxy service role trust policy carries no confused-deputy conditions | — |
| 33 | minor | The stack cannot be torn down: the secret is protected by an auto-generated account-wide DeleteSecret deny while carrying DeletionPolicy: Delete | — |
| 34 | minor | DBProxyName is hardcoded to "Proxy", so the stack cannot be instantiated twice in one account and region | — |
| 54 | minor | EngineVersion pinned to "18.3" together with AutoMinorVersionUpgrade: true guarantees a future stack-update failure | — |
| 61 | minor | X-Ray interface endpoint and xray:Put* grants provision in-VPC X-Ray access that nothing in the repo can use, contradicting the docstring's "the SDK never appears in the bundle" | — |
| 71 | minor | Two of the six interface endpoints have no caller: nothing inside the VPC calls the CloudWatch Logs or X-Ray APIs | — |
| 74 | minor | IngestApi timeout is exactly the API Gateway integration cap, not under it | — |
