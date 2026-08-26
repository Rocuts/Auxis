"""The AWS target, designed in full (CLAUDE.md: synthesized and validated,
never deployed — no account, no budget, anti-goal #5).

Shape: the AWS IDP reference architecture, specialized to this pipeline.

    API Gateway -> ingest Lambda -> S3 (documents) + jobs row
                                 -> Step Functions (Distributed Map, one
                                    branch per document):
        extract (Textract)  -> map+verify (Bedrock) -> persist (RDS via
        Proxy)              -> adjudicate (Bedrock + RDS via Proxy)

Design rules carried over from the rest of the repo:

- The extraction router's economics hold here too: Textract is the OCR
  path; a text-layer document is extracted in-Lambda by pdfplumber at $0.
- The semantic layer is the bounded three-role amendment (ADR 012): the
  map+verify function runs the mapper and the independent verifier; the
  adjudicator drains the review queue post-persist.
- No NAT gateways: Lambdas live in isolated subnets and reach AWS services
  through VPC endpoints only — cheaper, and no path to the internet exists
  for a component that handles tax documents.
- Tracing is Lambda ACTIVE tracing — a platform setting, not a library, so
  no tracing SDK ships in the bundle. Stated carefully because the earlier
  wording ("Powertools Tracer ... the SDK never appears in the bundle") was
  self-contradicting: Powertools' Tracer is a wrapper over aws-xray-sdk and
  pulls it in, which anti-goal #6 forbids. Powertools is not a dependency of
  this project (see pyproject), so nothing here imports either one; the
  POWERTOOLS_* variables below configure Logger for the dependency layer a
  deploy pipeline would add. If structured tracing is ever wanted in code,
  the anti-goal points at OpenTelemetry, not Powertools Tracer.
- Handlers are addressed as ``tax_tables.aws.handlers.*`` inside the
  ``src/`` asset: real, unit-tested code (tests/aws pins every handler
  string in this template to a callable). The Textract and Bedrock
  adapters behind them are likewise real and fixture-tested. What remains
  deploy-time is the dependency layer build (README, honest limitations);
  nothing here implies a deployment that does not exist.

Every cdk-nag suppression is individually justified inline, next to the
resource it covers.
"""

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sns as sns
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from aws_cdk import aws_wafv2 as wafv2
from cdk_nag import NagSuppressions
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The nonstandard port every target of this project uses (docker-compose
#: publishes 5433 locally); also satisfies AwsSolutions-RDS11.
DB_PORT = 5433
#: RDS Proxy for PostgreSQL listens on 5432 ALWAYS, whatever port its
#: target instance uses — wiring clients to the instance port was audit
#: critical #2 (confirmed by four agents): nothing could ever connect.
PROXY_PORT = 5432
DB_NAME = "tax"
#: The master user. Owned by the proxy's secret and the rotation schedule;
#: application Lambdas NEVER authenticate as it (audit critical: master on
#: RDS PostgreSQL is an rds_superuser member).
DB_MASTER_USER = "tax_ingest"
#: The least-privilege application role the Lambdas IAM-auth into. Created
#: by the deploy-time migration step (GRANT on the records/jobs/review
#: tables only) — recorded in the README's honest-limitations section.
APP_DB_USER = "app_ingest"

#: The Distributed Map fan-out ceiling — the knob the README's bottleneck
#: section reasons about, alongside RDS Proxy connection pooling.
MAX_CONCURRENT_DOCUMENTS = 8

#: Names the Map Run, and with it the CloudWatch dimension its child
#: executions report under. AWS: child workflow executions emit metrics
#: with a labelled State Machine ARN of the form
#: ``…:stateMachine:{stateMachineName}/{MapRunLabel or UUID}``. Unlabelled,
#: that dimension is a per-run UUID and no alarm can name it at synth time
#: — which is why the failure alarm below exists only because this is set.
MAP_RUN_LABEL = "PerDocument"

#: Reserved concurrency for the public read path, over its own API Gateway
#: throttle (50 rps steady / 100 burst): enough that reads keep being served
#: while the fan-out holds its own reservation.
API_RESERVED_CONCURRENCY = 25

#: Bedrock model family for the three semantic roles. Scoped to Anthropic
#: foundation models; per-role overrides ride Lambda env, mirroring the
#: SCHEMA_MAPPER_* / RECORD_VERIFIER_* / ADJUDICATOR_* chains.
BEDROCK_MODEL_ID = "anthropic.claude-opus-5"


class TaxTablesStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(
            scope, construct_id, description="Tax table ingestion service (designed; synth-only)"
        )

        # -- Network: isolated subnets, endpoints instead of NAT ----------
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=0,
            restrict_default_security_group=True,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="isolated",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                )
            ],
        )
        isolated = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED)

        logs_bucket = s3.Bucket(
            self,
            "AccessLogs",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            # Enforced ownership makes CDK grant log delivery via bucket
            # policy instead of the legacy ACL property (cfn-lint W3045).
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            lifecycle_rules=[s3.LifecycleRule(expiration=cdk.Duration.days(90))],
        )

        vpc.add_flow_log(
            "FlowLog",
            destination=ec2.FlowLogDestination.to_s3(logs_bucket, "vpc-flow-logs/"),
        )

        # Every endpoint carries a policy. Without one AWS applies full
        # access, and the S3 gateway endpoint then satisfies a PUT to any
        # bucket in ANY account — an egress path out of a VPC whose whole
        # point (docstring above) is that no path out exists. The condition
        # binds use of every endpoint to principals in this account, which
        # is what closes that path; it deliberately does not enumerate
        # actions, because an over-tight endpoint policy is a deploy-time
        # failure this project cannot test (README, honest limitations).
        same_account_only = iam.PolicyStatement(
            principals=[iam.AnyPrincipal()],
            actions=["*"],
            resources=["*"],
            conditions={"StringEquals": {"aws:PrincipalAccount": self.account}},
        )
        s3_endpoint = vpc.add_gateway_endpoint(
            "S3Endpoint", service=ec2.GatewayVpcEndpointAwsService.S3, subnets=[isolated]
        )
        s3_endpoint.add_to_policy(same_account_only)
        for name, service in (
            ("SecretsEndpoint", ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER),
            ("TextractEndpoint", ec2.InterfaceVpcEndpointAwsService.TEXTRACT),
            ("BedrockEndpoint", ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME),
            ("LogsEndpoint", ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS),
            ("StatesEndpoint", ec2.InterfaceVpcEndpointAwsService.STEP_FUNCTIONS),
            ("XRayEndpoint", ec2.InterfaceVpcEndpointAwsService.XRAY),
        ):
            vpc.add_interface_endpoint(name, service=service, subnets=isolated).add_to_policy(
                same_account_only
            )
        NagSuppressions.add_resource_suppressions(
            vpc,
            [
                {
                    "id": "CdkNagValidationFailure",
                    "appliesTo": ["AwsSolutions-EC23"],
                    "reason": "The endpoint security groups' only ingress is "
                    "allow-from-VPC-CIDR, expressed as a Fn::GetAtt token the "
                    "rule cannot resolve at synth. The VPC is fully isolated "
                    "(no IGW, no NAT), so the rule admits intra-VPC traffic "
                    "only by construction — 0.0.0.0/0 is unrepresentable here.",
                }
            ],
            apply_to_children=True,
        )

        # -- Documents bucket ---------------------------------------------
        documents = s3.Bucket(
            self,
            "Documents",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            server_access_logs_bucket=logs_bucket,
            server_access_logs_prefix="documents/",
            lifecycle_rules=[
                s3.LifecycleRule(
                    abort_incomplete_multipart_upload_after=cdk.Duration.days(2),
                    noncurrent_version_expiration=cdk.Duration.days(30),
                )
            ],
        )

        # Map Run exports get their own bucket rather than a prefix in the
        # access-log bucket: ResultWriterV2 grants the state machine
        # s3:PutObject on the WHOLE destination bucket, and pointing that at
        # the bucket holding VPC flow logs and S3 server access logs would
        # let the pipeline overwrite its own audit trail.
        map_run_results = s3.Bucket(
            self,
            "MapRunResults",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            server_access_logs_bucket=logs_bucket,
            server_access_logs_prefix="map-runs/",
            lifecycle_rules=[s3.LifecycleRule(expiration=cdk.Duration.days(90))],
        )

        # -- Database: RDS PostgreSQL + Proxy -----------------------------
        db_sg = ec2.SecurityGroup(self, "DbSg", vpc=vpc, allow_all_outbound=False)
        proxy_sg = ec2.SecurityGroup(self, "ProxySg", vpc=vpc, allow_all_outbound=False)
        lambda_sg = ec2.SecurityGroup(self, "LambdaSg", vpc=vpc, allow_all_outbound=True)
        # The hosted rotation Lambda gets its own group: it is the ONLY
        # thing besides the proxy allowed to reach the database directly
        # (audit finding, five agents independently: rotation in the shared
        # Lambda group could never reach the DB, so the 30-day schedule
        # would fail forever, silently).
        rotation_sg = ec2.SecurityGroup(self, "RotationSg", vpc=vpc, allow_all_outbound=True)
        db_sg.add_ingress_rule(proxy_sg, ec2.Port.tcp(DB_PORT), "proxy to database")
        db_sg.add_ingress_rule(rotation_sg, ec2.Port.tcp(DB_PORT), "hosted rotation to database")
        proxy_sg.add_ingress_rule(lambda_sg, ec2.Port.tcp(PROXY_PORT), "lambdas to proxy")
        proxy_sg.add_egress_rule(db_sg, ec2.Port.tcp(DB_PORT), "proxy to database")

        database = rds.DatabaseInstance(
            self,
            "Database",
            engine=rds.DatabaseInstanceEngine.postgres(
                # Pinned to the same major every other target runs (local
                # docker postgres:18, Neon 18.6).
                version=rds.PostgresEngineVersion.VER_18_3
            ),
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE4_GRAVITON, ec2.InstanceSize.MEDIUM
            ),
            vpc=vpc,
            vpc_subnets=isolated,
            security_groups=[db_sg],
            port=DB_PORT,
            database_name=DB_NAME,
            credentials=rds.Credentials.from_generated_secret(DB_MASTER_USER),
            multi_az=True,
            storage_encrypted=True,
            deletion_protection=True,
            backup_retention=cdk.Duration.days(7),
            cloudwatch_logs_exports=["postgresql"],
            auto_minor_version_upgrade=True,
        )
        secret = database.secret
        assert secret is not None  # from_generated_secret always creates one
        secret.add_rotation_schedule(
            "Rotation",
            automatically_after=cdk.Duration.days(30),
            hosted_rotation=secretsmanager.HostedRotation.postgre_sql_single_user(
                vpc=vpc, vpc_subnets=isolated, security_groups=[rotation_sg]
            ),
        )

        proxy = rds.DatabaseProxy(
            self,
            "Proxy",
            proxy_target=rds.ProxyTarget.from_instance(database),
            secrets=[secret],
            vpc=vpc,
            vpc_subnets=isolated,
            security_groups=[proxy_sg],
            require_tls=True,
            iam_auth=True,
            # The fan-out mitigation, actually configured. Defaults left
            # ConnectionPoolConfigurationInfo empty, so the stack's stated
            # answer to connection exhaustion was a claim rather than a
            # setting. MaxConcurrency 8 x four DB-touching steps is the
            # worst-case concurrent borrower count; the pool is sized to
            # absorb it and to hand a connection back rather than pin it.
            max_connections_percent=90,
            max_idle_connections_percent=50,
            borrow_timeout=cdk.Duration.seconds(30),
        )

        # Re-assert CDK's own placeholder, and do it HERE — after the last
        # construct that touches this group.
        # SecurityGroup.addEgressRule calls removeNoTrafficRule() before
        # emitting an SG-peer rule as a separate CfnSecurityGroupEgress, so
        # every such rule (ours to DbSg, and the "IndirectPort" rule the
        # DatabaseProxy adds itself) deletes the inline 255.255.255.255/32
        # placeholder and puts nothing inline in its place. AWS is explicit
        # about what that means: "The default rule is removed only when you
        # specify one or more egress rules." Without this line the proxy
        # gets allow-all egress despite allow_all_outbound=False — the
        # source said one thing and the template did another. An
        # inline-able peer/port lands in directEgressRules rather than a
        # separate resource, so the rule survives; a test pins it, because
        # any construct added below that touches proxy_sg would strip it
        # again just as silently.
        proxy_sg.add_egress_rule(
            ec2.Peer.ipv4("255.255.255.255/32"),
            ec2.Port.icmp_type_and_code(252, 86),
            "Disallow all traffic",
        )

        # -- Lambda functions ---------------------------------------------
        # The src/ tree IS the asset — the application code these handlers
        # import is real and shipped. What does NOT exist is the runtime
        # dependency layer (psycopg, pydantic, anthropic, boto3, mangum):
        # producing it is a deploy-pipeline build step, and this stack is
        # never deployed. Stated, not implied (audit finding); recorded in
        # the README's honest-limitations section.
        code = lambda_.Code.from_asset(
            str(REPO_ROOT / "src"), exclude=["**/__pycache__", "**/*.pyc"]
        )

        def function(
            name: str,
            handler: str,
            *,
            timeout_seconds: int,
            memory_mb: int,
            reserved: int,
            env: dict[str, str] | None = None,
        ) -> lambda_.Function:
            log_group = logs.LogGroup(
                self,
                f"{name}Logs",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            )
            fn = lambda_.Function(
                self,
                name,
                runtime=lambda_.Runtime.PYTHON_3_12,
                architecture=lambda_.Architecture.ARM_64,
                code=code,
                handler=handler,
                timeout=cdk.Duration.seconds(timeout_seconds),
                memory_size=memory_mb,
                # Reserved concurrency is both a floor and a ceiling, and
                # both directions matter here: MaxConcurrency bounds one Map
                # Run, not the account, so without this a large batch draws
                # the shared unreserved pool down and the public read path
                # starts returning 429s — while a burst of reads can equally
                # stall the pipeline. Sized in the README's bottleneck
                # section; the pipeline steps get the fan-out width, the API
                # gets headroom over its own gateway throttle.
                reserved_concurrent_executions=reserved,
                vpc=vpc,
                vpc_subnets=isolated,
                security_groups=[lambda_sg],
                tracing=lambda_.Tracing.ACTIVE,
                log_group=log_group,
                environment={
                    # Powertools Logger configuration (not Tracer — see
                    # the module docstring: Tracer would pull in the X-Ray
                    # SDK anti-goal #6 forbids). Tracing is the platform's,
                    # set by `tracing=ACTIVE` below.
                    "POWERTOOLS_SERVICE_NAME": "tax-tables",
                    "POWERTOOLS_LOG_LEVEL": "INFO",
                    "DB_PROXY_ENDPOINT": proxy.endpoint,
                    "DB_PORT": str(PROXY_PORT),
                    "DB_NAME": DB_NAME,
                    "DB_USER": APP_DB_USER,
                    "DOCUMENTS_BUCKET": documents.bucket_name,
                    **(env or {}),
                },
            )
            NagSuppressions.add_resource_suppressions(
                fn,
                [
                    {
                        "id": "AwsSolutions-L1",
                        "reason": "Runtime pinned to Python 3.12 — the interpreter "
                        "the entire project is developed, typed (mypy strict), and "
                        "CI-tested on (requires-python >=3.12). Moving the fleet to "
                        "a newer runtime before the test matrix moves is the actual "
                        "technical debt this rule warns about; revisited on every "
                        "dependency refresh.",
                    }
                ],
            )
            return fn

        api_fn = function(
            "IngestApi",
            "tax_tables.aws.handlers.api",
            timeout_seconds=29,  # under the API Gateway integration cap
            memory_mb=1024,
            reserved=API_RESERVED_CONCURRENCY,
        )
        extract_fn = function(
            "Extract",
            "tax_tables.aws.handlers.extract_document",
            timeout_seconds=300,
            memory_mb=2048,
            reserved=MAX_CONCURRENT_DOCUMENTS,
            env={"EXTRACTION_OCR_ENGINE": "textract"},
        )
        semantic_fn = function(
            "MapAndVerify",
            "tax_tables.aws.handlers.map_and_verify",
            timeout_seconds=900,  # document 03 maps 50+ records
            memory_mb=1024,
            reserved=MAX_CONCURRENT_DOCUMENTS,
            env={
                "SCHEMA_MAPPER_MODEL": BEDROCK_MODEL_ID,
                # The verifier may run a different-family model (ADR 012's
                # conformity mitigation); same chain as every other target.
                "RECORD_VERIFIER_MODEL": BEDROCK_MODEL_ID,
            },
        )
        persist_fn = function(
            "Persist",
            "tax_tables.aws.handlers.persist_records",
            timeout_seconds=120,
            memory_mb=1024,
            reserved=MAX_CONCURRENT_DOCUMENTS,
        )
        adjudicate_fn = function(
            "Adjudicate",
            "tax_tables.aws.handlers.adjudicate_queue",
            timeout_seconds=600,  # one call per open queue item
            memory_mb=1024,
            reserved=MAX_CONCURRENT_DOCUMENTS,
            env={"ADJUDICATOR_MODEL": BEDROCK_MODEL_ID},
        )

        # The Catch target for every pipeline step. Small and DB-only: its
        # single job is to make a failed document visible (see the fan-out
        # justification below).
        mark_failed_fn = function(
            "MarkFailed",
            "tax_tables.aws.handlers.mark_job_failed",
            timeout_seconds=30,
            memory_mb=512,
            reserved=MAX_CONCURRENT_DOCUMENTS,
        )

        documents.grant_put(api_fn)
        documents.grant_read(extract_fn)
        extract_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["textract:AnalyzeDocument"],
                # Textract supports no resource-level permissions; "*" is
                # the narrowest possible grant (suppression below).
                resources=["*"],
            )
        )
        bedrock_models = iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            # Scoped to Anthropic foundation models; the region wildcard is
            # required because cross-region inference profiles resolve to
            # regional model ARNs (suppression below).
            resources=[f"arn:{self.partition}:bedrock:*::foundation-model/anthropic.*"],
        )
        semantic_fn.add_to_role_policy(bedrock_models)
        adjudicate_fn.add_to_role_policy(bedrock_models)
        for fn in (api_fn, persist_fn, adjudicate_fn, mark_failed_fn):
            proxy.grant_connect(fn, APP_DB_USER)

        # -- Step Functions: Distributed Map fan-out ----------------------
        def invoke(name: str, fn: lambda_.Function) -> tasks.LambdaInvoke:
            task = tasks.LambdaInvoke(
                self,
                name,
                lambda_function=fn,
                payload_response_only=True,
                retry_on_service_exceptions=True,
            )
            # `retry_on_service_exceptions` covers exactly four errors in
            # aws-cdk-lib 2.266 — ClientExecutionTimeout, Service,
            # AWSLambda, SdkClient (verified against
            # aws-stepfunctions-tasks/lib/lambda/invoke.js). A throttle is
            # not among them, and a throttle is the EXPECTED failure at
            # MaxConcurrency 8 against an account-wide concurrency pool.
            # Unretried, an ordinary burst costs a whole document.
            task.add_retry(
                errors=["Lambda.TooManyRequestsException"],
                interval=cdk.Duration.seconds(5),
                max_attempts=6,
                backoff_rate=2,
            )
            return task

        steps = [
            invoke("ExtractStep", extract_fn),
            invoke("MapAndVerifyStep", semantic_fn),
            invoke("PersistStep", persist_fn),
            invoke("AdjudicateStep", adjudicate_fn),
        ]
        # The failure path. Record the reason against the job row, THEN
        # fail the child execution: marking the job alone would leave the
        # item counted as succeeded, and the batch-level metric below would
        # never see it.
        on_failure = invoke("MarkFailedStep", mark_failed_fn).next(
            sfn.Fail(
                self,
                "DocumentFailed",
                error="DocumentPipelineFailed",
                cause="A pipeline step failed; jobs.error carries the reason.",
            )
        )
        for step in steps:
            step.add_catch(on_failure, errors=["States.ALL"], result_path="$.error")
        per_document = steps[0].next(steps[1]).next(steps[2]).next(steps[3])

        fan_out = sfn.DistributedMap(
            self,
            "PerDocument",
            # Names the Map Run so its child executions report under a
            # dimension this stack can alarm on (MAP_RUN_LABEL).
            label=MAP_RUN_LABEL,
            # The bottleneck knob: concurrent branches multiply Bedrock
            # TPS and proxy connections; 8 is sized in the README's
            # bottleneck section.
            max_concurrency=MAX_CONCURRENT_DOCUMENTS,
            items_path="$.documents",
            #
            # ---- Why the fan-out tolerates 100% failure ----------------
            #
            # AWS is precise about what this setting does: "The default
            # percentage value is zero, which means that the workflow fails
            # if any one of its child workflow executions fails or times
            # out. If you specify the percentage as 100, the workflow won't
            # fail even if all child workflow executions fail."
            #
            # That is chosen, not conceded. The Map is TRANSPORT: it exists
            # to run N documents concurrently, and one malformed PDF in a
            # batch of 500 must not abort the 499 that are fine (the audit
            # critical this replaced). Batch-atomic semantics would be
            # actively wrong here — documents are independent, and a
            # rejected one is a data-quality event, not a system fault.
            #
            # The setting is therefore paired, never standalone. Because
            # the Map Run's own status is uninformative by construction,
            # failure has to be legible somewhere else, at both grains:
            #
            #   per document — the `jobs` row is the source of truth. Every
            #     step's Catch routes to MarkFailedStep, which writes
            #     status='failed' with the reason, and `GET /jobs/{id}`
            #     serves it. Without that Catch this setting would convert
            #     a loud batch abort into a job stuck at 'running' forever:
            #     silent loss, the worst failure mode this product defines
            #     (anti-goal #8).
            #
            #   per batch — the child executions of a labelled Map Run emit
            #     AWS/States ExecutionsFailed under
            #     `<state-machine-arn>/PerDocument`; one datapoint per
            #     failed document. DocumentFailuresAlarm (below) watches it
            #     and notifies the PipelineAlerts topic. The per-run report
            #     is the Map Run's own item counts — Failed/Aborted/
            #     Pending/Succeeded via DescribeMapRun and the Map Run
            #     Details page.
            #
            # So: the Map never fails, every failed document does — once in
            # the database the API serves, once in a metric an operator is
            # paged on.
            tolerated_failure_percentage=100,
            # The per-batch report named in that justification. Without it,
            # every child execution's output aggregates into the parent's
            # 256 KiB state payload, and the record of which documents
            # failed exists only in a Map Run the console ages out at 90
            # days. Exported, the batch outcome is a durable object.
            # Requires the @aws-cdk/aws-stepfunctions:useDistributedMapResultWriterV2
            # context flag (set in cdk.json): without it CDK accepts this
            # argument and silently emits no ResultWriter at all — verified,
            # the ASL had none until the flag went in.
            result_writer_v2=sfn.ResultWriterV2(bucket=map_run_results),
        )
        fan_out.item_processor(per_document)

        sfn_logs = logs.LogGroup(
            self,
            "PipelineLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        state_machine = sfn.StateMachine(
            self,
            "Pipeline",
            definition_body=sfn.DefinitionBody.from_chainable(fan_out),
            logs=sfn.LogOptions(destination=sfn_logs, level=sfn.LogLevel.ALL),
            tracing_enabled=True,
        )
        state_machine.grant_start_execution(api_fn)
        api_fn.add_environment("PIPELINE_STATE_MACHINE_ARN", state_machine.state_machine_arn)

        # -- Failure visibility -------------------------------------------
        # The other half of tolerated_failure_percentage=100 (justified at
        # the Map above): the jobs table carries the per-document truth,
        # these carry the batch-level signal.
        alerts = sns.Topic(
            self,
            "PipelineAlerts",
            display_name="Tax table pipeline failures",
            # The AWS-managed SNS key: an alias reference, not a lookup —
            # nothing here calls an AWS API at synth time (anti-goal #4).
            master_key=kms.Alias.from_alias_name(self, "SnsManagedKey", "alias/aws/sns"),
            enforce_ssl=True,
        )
        failed_documents = cloudwatch.Alarm(
            self,
            "DocumentFailures",
            metric=cloudwatch.Metric(
                namespace="AWS/States",
                metric_name="ExecutionsFailed",
                # Child executions of a labelled Map Run report under
                # `<state-machine-arn>/<label>` — one datapoint per failed
                # document. The parent execution stays green (the Map
                # tolerates the failure), so this is the ONLY metric that
                # shows a document was lost.
                dimensions_map={
                    "StateMachineArn": f"{state_machine.state_machine_arn}/{MAP_RUN_LABEL}"
                },
                statistic="Sum",
                period=cdk.Duration.minutes(5),
            ),
            threshold=0,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description=(
                "One or more documents failed inside the fan-out. The Map "
                "tolerates them by design; jobs.error carries each reason."
            ),
        )
        failed_documents.add_alarm_action(cw_actions.SnsAction(alerts))
        pipeline_failures = cloudwatch.Alarm(
            self,
            "PipelineFailures",
            # The parent execution: transport itself broke (bad input
            # shape, IAM, the Map never started). Disjoint from the alarm
            # above, which fires while the parent succeeds.
            metric=state_machine.metric_failed(period=cdk.Duration.minutes(5), statistic="Sum"),
            threshold=0,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="A pipeline execution failed outside the per-document fan-out.",
        )
        pipeline_failures.add_alarm_action(cw_actions.SnsAction(alerts))

        # -- API Gateway ---------------------------------------------------
        apigw_logs = logs.LogGroup(
            self,
            "ApiAccessLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        api = apigw.LambdaRestApi(
            self,
            "Api",
            handler=api_fn,
            proxy=True,
            # Without this, API Gateway UTF-8 mangles every PDF body before
            # the handler sees a byte (audit critical).
            binary_media_types=["application/pdf"],
            deploy_options=apigw.StageOptions(
                stage_name="v1",
                access_log_destination=apigw.LogGroupLogDestination(apigw_logs),
                access_log_format=apigw.AccessLogFormat.json_with_standard_fields(
                    caller=False,
                    http_method=True,
                    ip=True,
                    protocol=True,
                    request_time=True,
                    resource_path=True,
                    response_length=True,
                    status=True,
                    user=False,
                ),
                logging_level=apigw.MethodLoggingLevel.INFO,
                metrics_enabled=True,
                tracing_enabled=True,
                throttling_rate_limit=50,
                throttling_burst_limit=100,
            ),
        )

        # WAF: managed common rules plus a rate limit — the platform twin
        # of the Vercel Firewall rule in the CLAUDE.md hardening list.
        web_acl = wafv2.CfnWebACL(
            self,
            "WebAcl",
            scope="REGIONAL",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name="taxTablesWebAcl",
                sampled_requests_enabled=True,
            ),
            rules=[
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSManagedRulesCommonRuleSet",
                    priority=0,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS",
                            name="AWSManagedRulesCommonRuleSet",
                            # SizeRestrictions_BODY blocks any body over
                            # 8 KB — i.e. every real PDF upload (audit
                            # critical). Count, don't block: the app's own
                            # guards enforce the 10 MB cap, the %PDF magic,
                            # and the page cap before a byte reaches the
                            # pipeline (Phase 3, contract-tested), and API
                            # Gateway hard-caps payloads at 10 MB anyway.
                            rule_action_overrides=[
                                wafv2.CfnWebACL.RuleActionOverrideProperty(
                                    name="SizeRestrictions_BODY",
                                    action_to_use=wafv2.CfnWebACL.RuleActionProperty(count={}),
                                )
                            ],
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="commonRules",
                        sampled_requests_enabled=True,
                    ),
                ),
                wafv2.CfnWebACL.RuleProperty(
                    name="RateLimitPerIp",
                    priority=1,
                    action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                            aggregate_key_type="IP", limit=300
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="rateLimit",
                        sampled_requests_enabled=True,
                    ),
                ),
            ],
        )
        wafv2.CfnWebACLAssociation(
            self,
            "WebAclAssociation",
            resource_arn=api.deployment_stage.stage_arn,
            web_acl_arn=web_acl.attr_arn,
        )

        # -- Outputs --------------------------------------------------------
        cdk.CfnOutput(self, "ApiUrl", value=api.url)
        cdk.CfnOutput(self, "DocumentsBucket", value=documents.bucket_name)
        cdk.CfnOutput(self, "PipelineArn", value=state_machine.state_machine_arn)

        self._suppress_findings(api)

    def _suppress_findings(self, api: apigw.LambdaRestApi) -> None:
        """Every remaining nag finding, suppressed with its written reason.

        Stack-level suppressions are used only where the finding recurs on
        CDK-generated roles/policies whose construct paths are an
        implementation detail; each reason states the precise scope.
        """
        NagSuppressions.add_stack_suppressions(
            self,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    # Stated from the published policy documents, not from
                    # their names — the previous wording ("log-write only",
                    # "no privilege reduction") was false for all three, and
                    # a suppression whose justification is wrong is worse
                    # than no suppression. The API Gateway role is no longer
                    # excused here; it has its own, accurate reason below.
                    "reason": "The two Lambda service-role policies, quoted: "
                    "AWSLambdaBasicExecutionRole grants logs:CreateLogGroup / "
                    "CreateLogStream / PutLogEvents on Resource '*', and "
                    "AWSLambdaVPCAccessExecutionRole grants those same three "
                    "plus ec2:CreateNetworkInterface / DescribeNetworkInterfaces "
                    "/ DescribeSubnets / DeleteNetworkInterface / "
                    "(Un)AssignPrivateIpAddresses, also on '*'. Two honest "
                    "consequences: (a) the ENI half genuinely cannot be "
                    "narrowed — the ec2 Describe* calls support no "
                    "resource-level permissions, so an inline copy would carry "
                    "the identical wildcard; (b) the logs half COULD be "
                    "narrowed, because every function in this stack has an "
                    "explicit LogGroup, so '*' is wider than needed. It is "
                    "accepted because the actions are append-only (no "
                    "GetLogEvents, no FilterLogEvents, no Delete*): the worst "
                    "case is writing to another log group in the same account, "
                    "never reading or destroying one. Narrowing it means "
                    "replacing CDK's default role on six functions; recorded "
                    "as an open hardening item in the README rather than "
                    "claimed as done. Note also that Basic is wholly subsumed "
                    "by VPCAccess — CDK attaches both, and removing the "
                    "redundant one changes no effective permission.",
                    "appliesTo": [
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole",
                    ],
                },
                {
                    "id": "AwsSolutions-IAM5",
                    # Enumerated, not blanket. Without `appliesTo` this one
                    # entry silences every AwsSolutions-IAM5 finding in the
                    # stack — including wildcard grants nobody has written
                    # yet, which is the opposite of what a reviewed
                    # suppression is for. Listing the findings means the next
                    # wildcard fails the synth until someone justifies it.
                    # The list embeds CDK logical IDs, so a refactor that
                    # renames a construct breaks CI loudly; that cost is the
                    # feature.
                    "reason": "Four wildcard classes, each the narrowest expressible "
                    "grant. (1) textract:AnalyzeDocument supports no "
                    "resource-level permissions, so Resource '*' is mandatory. "
                    "(2) The Bedrock grant is scoped to "
                    "foundation-model/anthropic.* with a region wildcard "
                    "because cross-region inference profiles resolve to "
                    "regional model ARNs. (3) CDK grant helpers emit the "
                    "service-documented shapes: object-suffix "
                    "(<bucket-arn>/* for the documents bucket, and the whole "
                    "MapRunResults bucket — which is why the Map Run export got a "
                    "bucket of its own rather than a prefix in the audit-log "
                    "bucket), the s3 "
                    "Action:: families behind grant_read / grant_put, and "
                    "Lambda version suffixes (<function-arn>:*) for "
                    "lambda:InvokeFunction. (4) The DistributedMap "
                    "child-execution wildcards (execution:<name>:* and "
                    "execution:<name>/PerDocument:*) are the ARN shape Step "
                    "Functions documents for StartExecution and "
                    "DescribeExecution against a Map Run's children.",
                    "appliesTo": [
                        "Action::s3:Abort*",
                        "Action::s3:GetBucket*",
                        "Action::s3:GetObject*",
                        "Action::s3:List*",
                        "Resource::*",
                        "Resource::<AdjudicateB1101833.Arn>:*",
                        "Resource::<Documents7E5B2978.Arn>/*",
                        "Resource::<Extract9BA700EA.Arn>:*",
                        "Resource::<MapAndVerifyF30EE2AA.Arn>:*",
                        "Resource::<MarkFailed107085D9.Arn>:*",
                        "Resource::<Persist419B710D.Arn>:*",
                        "Resource::arn:<AWS::Partition>:bedrock:*::foundation-model/anthropic.*",
                        "Resource::arn:<AWS::Partition>:s3:::<MapRunResults8656708D>/*",
                        'Resource::arn:<AWS::Partition>:states:<AWS::Region>:<AWS::AccountId>:execution:{"Fn::Select":[6,{"Fn::Split":[":",{"Ref":"PipelineC660917D"}]}]}/PerDocument:*',
                        'Resource::arn:<AWS::Partition>:states:<AWS::Region>:<AWS::AccountId>:execution:{"Fn::Select":[6,{"Fn::Split":[":",{"Ref":"PipelineC660917D"}]}]}:*',
                    ],
                },
            ],
        )
        # The API Gateway account role, on its own accurate terms — it is
        # NOT log-write only, which is what the old shared reason claimed.
        NagSuppressions.add_resource_suppressions_by_path(
            self,
            "/TaxTables/Api/CloudWatchRole/Resource",
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": "AmazonAPIGatewayPushToCloudWatchLogs, quoted from its "
                    "published policy document, grants logs:CreateLogGroup, "
                    "CreateLogStream, DescribeLogGroups, DescribeLogStreams, "
                    "PutLogEvents, GetLogEvents and FilterLogEvents on Resource "
                    "'*'. The last two are account-wide log READ — materially "
                    "more than the write access the policy's name and "
                    "description suggest, and the reason this suppression is "
                    "separate from the Lambda one. It is accepted because the "
                    "role is assumable only by apigateway.amazonaws.com "
                    "(service principal, single trust statement), it is the "
                    "policy AWS requires on the account-level CloudWatch role, "
                    "and the alternative is turning off the API access logging "
                    "the same stack configures. The account-scoped blast radius "
                    "is stated, not hidden.",
                    "appliesTo": [
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
                    ],
                }
            ],
        )

        # API Gateway findings, scoped to the API construct only.
        NagSuppressions.add_resource_suppressions(
            api,
            [
                {
                    "id": "AwsSolutions-APIG2",
                    "reason": "Proxy integration: every request forwards verbatim to "
                    "the FastAPI app, where Pydantic validates bodies and query "
                    "parameters strictly (Phase 3 contract tests). A gateway "
                    "request validator on {proxy+} ANY validates nothing.",
                },
                {
                    "id": "AwsSolutions-APIG4",
                    "reason": "By design (CLAUDE.md hardening): GET endpoints are "
                    "public, read-only tax data; the write path enforces "
                    "X-API-Key in the app with a constant-time compare, backed "
                    "by WAF managed rules and a per-IP rate limit at the edge. "
                    "An IAM/Cognito authorizer would gate the public reads the "
                    "product requires open.",
                },
                {
                    "id": "AwsSolutions-COG4",
                    "reason": "Same rationale as APIG4: no user pool exists in this "
                    "design; auth is app-level API key on the single write "
                    "route.",
                },
            ],
            apply_to_children=True,
        )
