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
- Tracing is Powertools Tracer over Lambda active tracing (anti-goal #6:
  the X-Ray SDK is in maintenance; the SDK never appears in the bundle).
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
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
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

        vpc.add_gateway_endpoint(
            "S3Endpoint", service=ec2.GatewayVpcEndpointAwsService.S3, subnets=[isolated]
        )
        for name, service in (
            ("SecretsEndpoint", ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER),
            ("TextractEndpoint", ec2.InterfaceVpcEndpointAwsService.TEXTRACT),
            ("BedrockEndpoint", ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME),
            ("LogsEndpoint", ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS),
            ("StatesEndpoint", ec2.InterfaceVpcEndpointAwsService.STEP_FUNCTIONS),
            ("XRayEndpoint", ec2.InterfaceVpcEndpointAwsService.XRAY),
        ):
            vpc.add_interface_endpoint(name, service=service, subnets=isolated)
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
                vpc=vpc,
                vpc_subnets=isolated,
                security_groups=[lambda_sg],
                tracing=lambda_.Tracing.ACTIVE,
                log_group=log_group,
                environment={
                    # Powertools Tracer/Logger, never the X-Ray SDK
                    # (anti-goal #6).
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
        )
        extract_fn = function(
            "Extract",
            "tax_tables.aws.handlers.extract_document",
            timeout_seconds=300,
            memory_mb=2048,
            env={"EXTRACTION_OCR_ENGINE": "textract"},
        )
        semantic_fn = function(
            "MapAndVerify",
            "tax_tables.aws.handlers.map_and_verify",
            timeout_seconds=900,  # document 03 maps 50+ records
            memory_mb=1024,
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
        )
        adjudicate_fn = function(
            "Adjudicate",
            "tax_tables.aws.handlers.adjudicate_queue",
            timeout_seconds=600,  # one call per open queue item
            memory_mb=1024,
            env={"ADJUDICATOR_MODEL": BEDROCK_MODEL_ID},
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
        for fn in (api_fn, persist_fn, adjudicate_fn):
            proxy.grant_connect(fn, APP_DB_USER)

        # -- Step Functions: Distributed Map fan-out ----------------------
        def invoke(name: str, fn: lambda_.Function) -> tasks.LambdaInvoke:
            return tasks.LambdaInvoke(
                self,
                name,
                lambda_function=fn,
                payload_response_only=True,
                retry_on_service_exceptions=True,
            )

        per_document = (
            invoke("ExtractStep", extract_fn)
            .next(invoke("MapAndVerifyStep", semantic_fn))
            .next(invoke("PersistStep", persist_fn))
            .next(invoke("AdjudicateStep", adjudicate_fn))
        )
        fan_out = sfn.DistributedMap(
            self,
            "PerDocument",
            # The bottleneck knob: concurrent branches multiply Bedrock
            # TPS and proxy connections; 8 is sized in the README's
            # bottleneck section.
            max_concurrency=MAX_CONCURRENT_DOCUMENTS,
            items_path="$.documents",
            # One bad document must never abort the batch (audit critical):
            # failed items are recorded in the map result while siblings
            # finish — the same per-document isolation the local pipeline
            # and the accuracy gate enforce.
            tolerated_failure_percentage=100,
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
                    "reason": "AWSLambdaBasicExecutionRole / AWSLambdaVPCAccessExecutionRole "
                    "on CDK-generated Lambda roles: log-write and ENI-management "
                    "permissions only, the documented minimal baseline for VPC "
                    "Lambdas; replacing them with inline copies duplicates "
                    "AWS-maintained policy with no privilege reduction. Also "
                    "covers the API Gateway account CloudWatch role "
                    "(AmazonAPIGatewayPushToCloudWatchLogs), which is log-write "
                    "only.",
                    "appliesTo": [
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole",
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs",
                    ],
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "Three wildcard classes, each the narrowest expressible "
                    "grant: (1) textract:AnalyzeDocument supports no "
                    "resource-level permissions, so '*' is mandatory; (2) the "
                    "Bedrock grant is scoped to foundation-model/anthropic.* "
                    "with a region wildcard because cross-region inference "
                    "profiles resolve to regional model ARNs; (3) CDK grant "
                    "helpers emit object-suffix (<bucket-arn>/*), Lambda "
                    "version (:*), X-Ray/log-delivery, and DistributedMap "
                    "child-execution wildcards that are the service-documented "
                    "shapes for those grants.",
                },
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
