"""Template assertions pinning the confirmed findings of the Phase 4
adversarial audit (14 confirmed, 0 refuted — see the dev-log). Each test
names the defect it forecloses; every one FAILED against the pre-audit
stack. Written test-first, per the audit's ground rule.

The suite synthesizes the stack in-process (aws_cdk.assertions), so it
needs the `infra` dependency group; a lean environment without it skips
rather than lies.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

aws_cdk = pytest.importorskip("aws_cdk", reason="infra dependency group not installed")

from aws_cdk import App  # noqa: E402
from aws_cdk.assertions import Match, Template  # noqa: E402

INFRA_DIR = Path(__file__).resolve().parents[2] / "infra"
sys.path.insert(0, str(INFRA_DIR))
from tax_tables_stack import TaxTablesStack  # noqa: E402


@pytest.fixture(scope="module")
def template() -> Template:
    # The same feature-flag context `cdk synth` runs under (cdk.json is the
    # single source of truth) — flags change what CDK emits, so a test app
    # without them would audit a template nobody synthesizes.
    context: dict[str, Any] = json.loads((INFRA_DIR / "cdk.json").read_text())["context"]
    return Template.from_stack(TaxTablesStack(App(context=context), "TaxTables"))


def _resources(template: Template, type_name: str) -> dict[str, Any]:
    return dict(template.find_resources(type_name))


class TestFlowLogDelivery:
    def test_access_log_bucket_grants_the_flow_log_delivery_principal(
        self, template: Template
    ) -> None:
        """Audit critical #1: without the delivery.logs.amazonaws.com
        statements, flow-log creation either fails the deploy or the
        service OVERWRITES the bucket policy, silently destroying the
        enforce-SSL Deny and the access-log grant (AWS docs verbatim)."""
        policies = _resources(template, "AWS::S3::BucketPolicy")
        statements = [
            statement
            for policy in policies.values()
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        ]
        delivery = [
            s
            for s in statements
            if s.get("Principal", {}).get("Service") == "delivery.logs.amazonaws.com"
        ]
        actions = {
            a
            for s in delivery
            for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])
        }
        assert "s3:PutObject" in actions, "flow-log delivery write grant missing"
        assert "s3:GetBucketAcl" in actions, "flow-log delivery acl-check grant missing"


class TestProxyPort:
    def test_lambdas_reach_the_proxy_on_5432(self, template: Template) -> None:
        """Audit critical #2 (found by four agents independently): RDS Proxy
        for PostgreSQL listens ONLY on 5432, whatever port the target
        instance uses. The lambda->proxy path must be 5432 or nothing can
        ever connect."""
        ingresses = _resources(template, "AWS::EC2::SecurityGroupIngress")
        proxy_rules = [
            r["Properties"]
            for r in ingresses.values()
            if "lambdas to proxy" in r["Properties"].get("Description", "")
        ]
        assert proxy_rules, "the lambdas-to-proxy ingress rule must exist"
        assert proxy_rules[0]["FromPort"] == 5432
        assert proxy_rules[0]["ToPort"] == 5432

    def test_lambda_env_db_port_is_the_proxy_listener(self, template: Template) -> None:
        functions = _resources(template, "AWS::Lambda::Function")
        app_fns = [
            f["Properties"]["Environment"]["Variables"]
            for f in functions.values()
            if "DB_PROXY_ENDPOINT" in f["Properties"].get("Environment", {}).get("Variables", {})
        ]
        # Six app functions since the failure path landed: api, extract,
        # map+verify, persist, adjudicate, mark-failed.
        assert len(app_fns) == 6
        assert all(env["DB_PORT"] == "5432" for env in app_fns)

    def test_proxy_still_reaches_the_instance_on_its_port(self, template: Template) -> None:
        ingresses = _resources(template, "AWS::EC2::SecurityGroupIngress")
        db_rules = [
            r["Properties"]
            for r in ingresses.values()
            if "proxy to database" in r["Properties"].get("Description", "")
        ]
        assert db_rules and db_rules[0]["FromPort"] == 5433


class TestRotationReachability:
    def test_database_admits_the_rotation_lambda(self, template: Template) -> None:
        """Found independently by five agents: the hosted rotation Lambda
        must reach the database directly (not via the proxy) or the 30-day
        rotation schedule fails forever, silently."""
        ingresses = _resources(template, "AWS::EC2::SecurityGroupIngress")
        rotation_rules = [
            r["Properties"]
            for r in ingresses.values()
            if "rotation" in r["Properties"].get("Description", "").lower()
        ]
        assert rotation_rules, "the rotation-to-database ingress rule must exist"
        assert rotation_rules[0]["FromPort"] == 5433


class TestDatabaseIdentity:
    def test_lambdas_never_authenticate_as_the_master_user(self, template: Template) -> None:
        """Audit critical: rds-db:connect grants must name the application
        role, not the master user the proxy and rotation own."""
        policies = _resources(template, "AWS::IAM::Policy")
        connect_resources: list[Any] = []
        for policy in policies.values():
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
                actions = statement["Action"]
                if (isinstance(actions, str) and actions == "rds-db:connect") or (
                    isinstance(actions, list) and "rds-db:connect" in actions
                ):
                    connect_resources.append(statement["Resource"])
        assert connect_resources, "rds-db:connect grants must exist"
        rendered = json.dumps(connect_resources)
        assert "app_ingest" in rendered
        assert "tax_ingest" not in rendered, "master user must not appear in connect grants"


class TestUploadPath:
    def test_rest_api_declares_pdf_as_binary(self, template: Template) -> None:
        """Audit critical: without BinaryMediaTypes, API Gateway UTF-8
        mangles every PDF body before the handler sees it."""
        template.has_resource_properties(
            "AWS::ApiGateway::RestApi",
            {"BinaryMediaTypes": Match.array_with(["application/pdf"])},
        )

    def test_waf_body_size_rule_is_count_not_block(self, template: Template) -> None:
        """Audit critical: SizeRestrictions_BODY blocks any body over 8 KB
        in Block mode — every real PDF upload. The app enforces its own
        10 MB cap before a byte reaches the pipeline (Phase 3 guards)."""
        acls = _resources(template, "AWS::WAFv2::WebACL")
        (acl,) = acls.values()
        managed = next(
            rule
            for rule in acl["Properties"]["Rules"]
            if rule["Name"] == "AWSManagedRulesCommonRuleSet"
        )
        overrides = managed["Statement"]["ManagedRuleGroupStatement"].get("RuleActionOverrides", [])
        assert {
            "Name": "SizeRestrictions_BODY",
            "ActionToUse": {"Count": {}},
        } in overrides


class TestPipelineReliability:
    def test_distributed_map_tolerates_per_document_failure(self, template: Template) -> None:
        """Audit critical: with no tolerated-failure setting, one bad
        document aborts the whole batch — the exact failure mode the
        local pipeline's per-document isolation exists to prevent."""
        machines = _resources(template, "AWS::StepFunctions::StateMachine")
        (machine,) = machines.values()
        definition = json.dumps(machine["Properties"]["DefinitionString"])
        assert "ToleratedFailurePercentage" in definition
        assert "100" in definition


def _definition(template: Template) -> dict[str, Any]:
    """The state machine's ASL, parsed.

    ``DefinitionString`` is an ``Fn::Join`` of literal JSON fragments and
    unresolved tokens (ARNs). Substituting a placeholder for every token
    yields valid JSON, so these tests assert on structure rather than on
    substring presence in a serialized blob.
    """
    (machine,) = _resources(template, "AWS::StepFunctions::StateMachine").values()
    body = machine["Properties"]["DefinitionString"]
    if isinstance(body, str):
        return dict(json.loads(body))
    parts = body["Fn::Join"][1]
    return dict(json.loads("".join(p if isinstance(p, str) else "TOKEN" for p in parts)))


def _per_document_states(template: Template) -> dict[str, Any]:
    definition = _definition(template)
    (map_state,) = [s for s in definition["States"].values() if s["Type"] == "Map"]
    return dict(map_state["ItemProcessor"]["States"])


class TestPerDocumentFailurePath:
    """The other half of ``tolerated_failure_percentage=100``.

    AWS documents the setting precisely: "If you specify the percentage as
    100, the workflow won't fail even if all child workflow executions
    fail." So the Map Run's own status carries no failure information by
    construction — which is only a design if some other artifact carries
    it. These tests pin the three places that do: the jobs table (per
    document), a failed child execution (per document, in CloudWatch), and
    an alarm over that metric (per batch).
    """

    def test_every_step_retries_lambda_throttling(self, template: Template) -> None:
        """``retry_on_service_exceptions`` covers exactly four errors in
        aws-cdk-lib 2.266 (verified by decompiling
        aws-stepfunctions-tasks/lib/lambda/invoke.js):
        ClientExecutionTimeout, Service, AWSLambda, SdkClient. A throttle
        is NOT among them — and a throttle is the *expected* failure at
        MaxConcurrency 8, so without this an ordinary burst silently
        drops a document."""
        states = _per_document_states(template)
        tasks = {name: s for name, s in states.items() if s["Type"] == "Task"}
        assert tasks, "no Task states in the item processor"
        for name, state in tasks.items():
            errors = {e for retrier in state.get("Retry", []) for e in retrier["ErrorEquals"]}
            assert "Lambda.TooManyRequestsException" in errors, name

    def test_every_pipeline_step_catches_into_the_failure_path(self, template: Template) -> None:
        """Without a Catch, a failed step ends the child execution with the
        job row still 'running' — GET /jobs/{id} reports a document as in
        progress forever, and the loss is invisible (anti-goal #8)."""
        states = _per_document_states(template)
        pipeline_steps = {
            name: s
            for name, s in states.items()
            if s["Type"] == "Task" and not name.startswith("MarkFailed")
        }
        assert len(pipeline_steps) == 4
        for name, state in pipeline_steps.items():
            catchers = state.get("Catch", [])
            assert catchers, f"{name} has no Catch"
            assert any("States.ALL" in c["ErrorEquals"] for c in catchers), name
            assert all(c["Next"].startswith("MarkFailed") for c in catchers), name

    def test_the_failure_path_records_then_fails_the_item(self, template: Template) -> None:
        """Marking the job is not enough: the child execution must still
        FAIL, or the item counts as succeeded and the batch-level metric
        below never fires."""
        states = _per_document_states(template)
        (mark_failed,) = [name for name in states if name.startswith("MarkFailed")]
        following = states[mark_failed]["Next"]
        assert states[following]["Type"] == "Fail"

    def test_map_run_is_labelled_so_child_failures_are_alarmable(self, template: Template) -> None:
        """Child executions emit metrics under
        ``stateMachine:{name}/{MapRunLabel or UUID}``. Unlabelled, the
        dimension is a per-run UUID and no alarm can name it at synth
        time."""
        definition = _definition(template)
        (map_state,) = [s for s in definition["States"].values() if s["Type"] == "Map"]
        assert map_state["Label"] == "PerDocument"

    def test_an_alarm_watches_failed_child_executions(self, template: Template) -> None:
        alarms = _resources(template, "AWS::CloudWatch::Alarm")
        child = [
            a
            for a in alarms.values()
            if a["Properties"].get("MetricName") == "ExecutionsFailed"
            and a["Properties"].get("Namespace") == "AWS/States"
            and any(
                "PerDocument" in json.dumps(d.get("Value"))
                for d in a["Properties"].get("Dimensions", [])
            )
        ]
        assert len(child) == 1, "no alarm on the labelled child-execution metric"
        (alarm,) = child
        assert alarm["Properties"]["AlarmActions"], "the alarm notifies nobody"

    def test_the_failure_path_can_reach_the_database(self, template: Template) -> None:
        """A mark-failed Lambda that cannot connect records nothing."""
        functions = _resources(template, "AWS::Lambda::Function")
        mark_failed = [
            name
            for name, fn in functions.items()
            if fn["Properties"].get("Handler") == "tax_tables.aws.handlers.mark_job_failed"
        ]
        assert len(mark_failed) == 1
        role = functions[mark_failed[0]]["Properties"]["Role"]["Fn::GetAtt"][0]
        connects = [
            policy
            for policy in _resources(template, "AWS::IAM::Policy").values()
            if any(
                "rds-db:connect" in json.dumps(statement.get("Action"))
                for statement in policy["Properties"]["PolicyDocument"]["Statement"]
            )
            and any(r["Ref"] == role for r in policy["Properties"].get("Roles", []))
        ]
        assert connects, "mark_job_failed cannot authenticate to the database"


class TestPromotedAuditFindings:
    """The audit named 75 findings and verified 14. This class covers the
    ones promoted afterwards by a title-level sweep against three criteria
    — data loss, money paths, auth — each then verified against primary
    sources (AWS documentation, or the decompiled aws-cdk-lib) rather than
    accepted on the finder's word. The rest stay unverified by design; the
    dev-log records where the full list lives.
    """

    def test_proxy_egress_is_not_the_default_allow_all(self, template: Template) -> None:
        """auth/network. `allow_all_outbound=False` did not survive synth.

        aws-cdk-lib 2.266's SecurityGroup.addEgressRule calls
        removeNoTrafficRule() BEFORE the branch that emits a peer rule as a
        separate CfnSecurityGroupEgress — so adding an SG-peer egress rule
        deletes the inline 255.255.255.255/32 placeholder and puts nothing
        inline in its place. AWS then applies its own default: "When you
        create a security group, if you do not add egress rules, we add
        egress rules that allow all outbound IPv4 and IPv6 traffic" — "The
        default rule is removed only when you specify one or more egress
        rules." The proxy got allow-all egress while the source said
        otherwise.
        """
        groups = _resources(template, "AWS::EC2::SecurityGroup")
        proxy_sgs = [
            g for name, g in groups.items() if name.startswith("ProxySg") and "Endpoint" not in name
        ]
        assert len(proxy_sgs) == 1
        egress = proxy_sgs[0]["Properties"].get("SecurityGroupEgress")
        assert egress, "no inline egress rule: AWS re-adds allow-all outbound"
        assert not any(rule.get("CidrIp") == "0.0.0.0/0" for rule in egress)

    def test_every_vpc_endpoint_restricts_who_may_use_it(self, template: Template) -> None:
        """auth. The stack's docstring claims no path to the internet
        exists for a component handling tax documents. A default
        full-access S3 gateway endpoint is exactly such a path: it will
        carry a PUT to any bucket in any account."""
        endpoints = _resources(template, "AWS::EC2::VPCEndpoint")
        assert len(endpoints) == 7
        for name, endpoint in endpoints.items():
            policy = endpoint["Properties"].get("PolicyDocument")
            assert policy, f"{name} uses the default full-access policy"
            conditions = json.dumps(policy)
            assert "aws:PrincipalAccount" in conditions, name

    def test_the_proxy_connection_pool_is_configured(self, template: Template) -> None:
        """money/bottleneck. RDS Proxy pooling is the stack's stated
        mitigation for connection exhaustion under fan-out; the target
        group shipped with an empty ConnectionPoolConfigurationInfo, so
        the mitigation was a claim, not a setting."""
        (group,) = _resources(template, "AWS::RDS::DBProxyTargetGroup").values()
        pool = group["Properties"]["ConnectionPoolConfigurationInfo"]
        assert pool.get("MaxConnectionsPercent")
        assert pool.get("MaxIdleConnectionsPercent") is not None
        assert pool.get("ConnectionBorrowTimeout")

    def test_the_fan_out_cannot_starve_the_read_path(self, template: Template) -> None:
        """money/availability. MaxConcurrency bounds one Map Run, not the
        account: without reserved concurrency the pipeline and the public
        GET path draw from the same unreserved pool, so a large batch can
        make the API return 429s — and a burst of reads can stall the
        pipeline."""
        functions = _resources(template, "AWS::Lambda::Function")
        app = {
            name: fn["Properties"]
            for name, fn in functions.items()
            if str(fn["Properties"].get("Handler", "")).startswith("tax_tables.")
        }
        assert len(app) == 6
        for name, props in app.items():
            assert props.get("ReservedConcurrentExecutions"), name

    def test_map_run_results_are_exported(self, template: Template) -> None:
        """data loss. Without a ResultWriter every child execution's output
        aggregates into the parent's 256 KiB state payload, and the
        per-batch record of which documents failed lives only in a Map Run
        the console ages out after 90 days."""
        definition = _definition(template)
        (map_state,) = [s for s in definition["States"].values() if s["Type"] == "Map"]
        assert "ResultWriter" in map_state
        # ResultWriterV2 emits no IAM grant of its own; an export the state
        # machine cannot perform is worse than none.
        writes = [
            policy
            for policy in _resources(template, "AWS::IAM::Policy").values()
            if any(
                "s3:PutObject" in json.dumps(statement.get("Action"))
                for statement in policy["Properties"]["PolicyDocument"]["Statement"]
            )
            and any("Pipeline" in json.dumps(r) for r in policy["Properties"].get("Roles", []))
        ]
        assert writes, "the state machine cannot write its Map Run results"

    def test_the_iam4_justification_states_what_the_policies_grant(
        self, template: Template
    ) -> None:
        """auth. The stack-level IAM4 reason called
        AmazonAPIGatewayPushToCloudWatchLogs "log-write only". Its
        published policy document grants logs:GetLogEvents and
        logs:FilterLogEvents on Resource "*" — account-wide log READ. A
        suppression whose justification is false is worse than none, and
        the Phase 4 gate is precisely about written justifications."""
        suppressions = template.to_json()["Metadata"]["cdk_nag"]["rules_to_suppress"]
        iam4 = [s for s in suppressions if s["id"] == "AwsSolutions-IAM4"]
        assert len(iam4) == 1
        applies = iam4[0]["applies_to"]
        assert not any("APIGatewayPushToCloudWatchLogs" in entry for entry in applies), (
            "the API Gateway role is still excused by a reason written about Lambda policies"
        )
        assert "log-write only" not in iam4[0]["reason"]

    def test_the_iam5_suppression_is_scoped(self, template: Template) -> None:
        """auth. An `appliesTo`-less stack suppression silences every
        AwsSolutions-IAM5 finding in the stack forever — including
        wildcard grants nobody has written yet. Enumerating the findings
        turns the next one into a failed synth instead of a silent pass."""
        suppressions = template.to_json()["Metadata"]["cdk_nag"]["rules_to_suppress"]
        iam5 = [s for s in suppressions if s["id"] == "AwsSolutions-IAM5"]
        assert len(iam5) == 1
        assert iam5[0].get("applies_to"), "unscoped: any future wildcard is pre-excused"
