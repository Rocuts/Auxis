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
        assert len(app_fns) == 5
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
