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
