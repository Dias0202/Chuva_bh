"""Verifica as regras de custo e segurança no template sintetizado."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import teardown
from app import (
    ENV_BUDGET_EMAIL,
    ENV_KILL_SWITCH_USERS,
    build_app,
    load_config,
)
from aws_cdk import App, Environment
from aws_cdk.assertions import Template
from stacks.common import REGION
from stacks.foundation_stack import FoundationConfig, FoundationStack

ACCOUNT = "111122223333"
EMAIL = "alerts@example.invalid"
REQUIRED_TAGS = {"Project": "chuva-bh", "Phase": "0", "Owner": "dias"}
# Tipos que o CloudFormation não permite marcar com tags.
UNTAGGABLE_TYPES = {
    "AWS::IAM::ManagedPolicy",
    "AWS::IAM::Policy",
    "AWS::KMS::Alias",
    "AWS::S3::BucketPolicy",
}
FORBIDDEN_TYPES = {
    "AWS::EC2::NatGateway",
    "AWS::EMR::Cluster",
    "AWS::Kinesis::Stream",
    "AWS::MSK::Cluster",
    "AWS::OpenSearchServerless::Collection",
    "AWS::OpenSearchService::Domain",
    "AWS::Redshift::Cluster",
    "AWS::SageMaker::Endpoint",
    "AWS::SageMaker::MlflowTrackingServer",
    "AWS::Bedrock::ProvisionedModelThroughput",
}


def synth(**config_overrides: Any) -> Template:
    config = FoundationConfig(**{"budget_email": EMAIL, **config_overrides})
    stack = FoundationStack(
        App(),
        "TestFoundation",
        env=Environment(account=ACCOUNT, region="us-east-2"),
        config=config,
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template() -> Template:
    return synth()


def resources_of(template: Template, resource_type: str) -> dict[str, dict[str, Any]]:
    return {
        logical_id: dict(resource)
        for logical_id, resource in template.find_resources(resource_type).items()
    }


def single_properties(template: Template, resource_type: str) -> dict[str, Any]:
    found = resources_of(template, resource_type)
    assert len(found) == 1
    properties: dict[str, Any] = next(iter(found.values()))["Properties"]
    return properties


def bucket_by_prefix(template: Template, name: str) -> dict[str, Any]:
    matches = [
        resource["Properties"]
        for resource in resources_of(template, "AWS::S3::Bucket").values()
        if resource["Properties"]["BucketName"].startswith(f"chuva-bh-{name}-")
    ]
    assert len(matches) == 1
    properties: dict[str, Any] = matches[0]
    return properties


def iam_statements(template: Template) -> Iterator[tuple[str, dict[str, Any]]]:
    for resource_type in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
        for logical_id, resource in resources_of(template, resource_type).items():
            for statement in resource["Properties"]["PolicyDocument"]["Statement"]:
                yield logical_id, statement


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def test_creates_the_four_data_buckets_and_the_audit_bucket(template: Template) -> None:
    names = {
        resource["Properties"]["BucketName"]
        for resource in resources_of(template, "AWS::S3::Bucket").values()
    }
    assert names == {
        f"chuva-bh-{name}-{ACCOUNT}-us-east-2"
        for name in ("raw", "curated", "artifacts", "kb-docs", "audit")
    }


def test_every_bucket_blocks_public_access_and_uses_the_project_key(template: Template) -> None:
    for resource in resources_of(template, "AWS::S3::Bucket").values():
        properties = resource["Properties"]
        assert properties["PublicAccessBlockConfiguration"] == {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }
        rule = properties["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
        assert rule["BucketKeyEnabled"] is True
        sse = rule["ServerSideEncryptionByDefault"]
        assert sse["SSEAlgorithm"] == "aws:kms"
        assert "Fn::GetAtt" in sse["KMSMasterKeyID"]


def test_every_bucket_denies_requests_without_tls(template: Template) -> None:
    policies = resources_of(template, "AWS::S3::BucketPolicy")
    assert len(policies) == 5
    for policy in policies.values():
        statements = policy["Properties"]["PolicyDocument"]["Statement"]
        assert any(
            statement["Effect"] == "Deny"
            and statement.get("Condition") == {"Bool": {"aws:SecureTransport": "false"}}
            for statement in statements
        )


@pytest.mark.parametrize(
    ("bucket", "expected_days"),
    [("raw", 30), ("audit", 90), ("curated", None), ("artifacts", None), ("kb-docs", None)],
)
def test_expiration_is_applied_only_where_planned(
    template: Template, bucket: str, expected_days: int | None
) -> None:
    rules = bucket_by_prefix(template, bucket)["LifecycleConfiguration"]["Rules"]
    expirations = [rule["ExpirationInDays"] for rule in rules if "ExpirationInDays" in rule]
    assert expirations == ([] if expected_days is None else [expected_days])
    assert any("AbortIncompleteMultipartUpload" in rule for rule in rules)


def test_key_rotates_and_can_be_removed_by_teardown(template: Template) -> None:
    keys = resources_of(template, "AWS::KMS::Key")
    assert len(keys) == 1
    key = next(iter(keys.values()))
    assert key["Properties"]["EnableKeyRotation"] is True
    assert key["Properties"]["PendingWindowInDays"] == 7
    assert key["DeletionPolicy"] == "Delete"


def test_trail_is_encrypted_multi_region_and_validated(template: Template) -> None:
    trail = single_properties(template, "AWS::CloudTrail::Trail")
    assert trail["IsMultiRegionTrail"] is True
    assert trail["IncludeGlobalServiceEvents"] is True
    assert trail["EnableLogFileValidation"] is True
    assert "KMSKeyId" in trail
    assert not trail.get("EventSelectors")


def test_key_policy_lets_only_the_project_trail_encrypt(template: Template) -> None:
    key = next(iter(resources_of(template, "AWS::KMS::Key").values()))
    statements = {
        statement["Sid"]: statement
        for statement in key["Properties"]["KeyPolicy"]["Statement"]
        if "Sid" in statement
    }
    encrypt = statements["AllowCloudTrailEncrypt"]
    assert encrypt["Principal"] == {"Service": "cloudtrail.amazonaws.com"}
    source_arn_parts = encrypt["Condition"]["StringEquals"]["aws:SourceArn"]["Fn::Join"][1]
    assert source_arn_parts == [
        "arn:",
        {"Ref": "AWS::Partition"},
        f":cloudtrail:us-east-2:{ACCOUNT}:trail/chuva-bh-trail",
    ]


def test_budget_counts_gross_cost_so_credits_do_not_hide_spending(template: Template) -> None:
    budget = single_properties(template, "AWS::Budgets::Budget")["Budget"]
    assert budget["BudgetType"] == "COST"
    assert budget["BudgetLimit"] == {"Amount": 60, "Unit": "USD"}
    assert budget["CostTypes"]["IncludeCredit"] is False
    assert budget["CostTypes"]["IncludeRefund"] is False


def test_budget_accumulates_over_the_whole_project(template: Template) -> None:
    budget = single_properties(template, "AWS::Budgets::Budget")["Budget"]
    assert budget["TimeUnit"] == "ANNUALLY"
    assert budget["TimePeriod"]["Start"] == "1793491200"


def test_budget_alerts_at_each_planned_threshold(template: Template) -> None:
    properties = single_properties(template, "AWS::Budgets::Budget")
    notifications = properties["NotificationsWithSubscribers"]
    assert sorted(item["Notification"]["Threshold"] for item in notifications) == [25, 50, 80, 100]
    for item in notifications:
        assert item["Notification"]["NotificationType"] == "ACTUAL"
        assert item["Notification"]["ThresholdType"] == "PERCENTAGE"
        assert item["Subscribers"] == [{"Address": EMAIL, "SubscriptionType": "EMAIL"}]


def test_kill_switch_fires_at_ninety_percent_without_manual_approval(template: Template) -> None:
    action = single_properties(template, "AWS::Budgets::BudgetsAction")
    assert action["ActionThreshold"] == {"Type": "PERCENTAGE", "Value": 90}
    assert action["ActionType"] == "APPLY_IAM_POLICY"
    assert action["ApprovalModel"] == "AUTOMATIC"
    assert action["NotificationType"] == "ACTUAL"


def test_kill_switch_targets_the_role_that_actually_creates_resources(template: Template) -> None:
    action = single_properties(template, "AWS::Budgets::BudgetsAction")
    roles = action["Definition"]["IamActionDefinition"]["Roles"]
    assert roles == [f"cdk-hnb659fds-cfn-exec-role-{ACCOUNT}-us-east-2"]


def test_kill_switch_policy_blocks_creation_but_not_deletion(template: Template) -> None:
    policy = single_properties(template, "AWS::IAM::ManagedPolicy")
    statement = policy["PolicyDocument"]["Statement"][0]
    actions = as_list(statement["Action"])
    assert statement["Effect"] == "Deny"
    assert {"sagemaker:Create*", "bedrock:Create*"} <= set(actions)
    assert not [action for action in actions if "Delete" in action or "Stop" in action]


def test_kill_switch_execution_role_can_only_attach_the_deny_policy(template: Template) -> None:
    statements = [
        statement
        for _, statement in iam_statements(template)
        if "Sid" in statement and statement["Sid"].startswith("ManageDenyPolicy")
    ]
    assert len(statements) == 1
    assert "iam:PolicyARN" in statements[0]["Condition"]["ArnEquals"]
    assert "*" not in as_list(statements[0]["Resource"])


def test_kill_switch_can_include_extra_iam_users() -> None:
    template = synth(kill_switch_users=("deployer",))
    action = single_properties(template, "AWS::Budgets::BudgetsAction")
    assert action["Definition"]["IamActionDefinition"]["Users"] == ["deployer"]
    sids = {statement.get("Sid") for _, statement in iam_statements(template)}
    assert "ManageDenyPolicyOnUsers" in sids


def test_kill_switch_omits_users_by_default(template: Template) -> None:
    action = single_properties(template, "AWS::Budgets::BudgetsAction")
    assert "Users" not in action["Definition"]["IamActionDefinition"]


def test_identity_policies_only_use_wildcard_on_the_deny_statement(template: Template) -> None:
    offenders = [
        logical_id
        for logical_id, statement in iam_statements(template)
        if "*" in as_list(statement["Resource"])
        and statement.get("Sid") != "DenyCostlyResourceCreation"
    ]
    assert offenders == []


def test_every_taggable_resource_has_the_required_tags(template: Template) -> None:
    resources = template.to_json()["Resources"]
    for logical_id, resource in resources.items():
        if resource["Type"] in UNTAGGABLE_TYPES:
            continue
        properties = resource["Properties"]
        raw_tags = properties.get("Tags", properties.get("ResourceTags"))
        assert raw_tags is not None, f"{logical_id} sem tags"
        tags = {tag["Key"]: tag["Value"] for tag in raw_tags}
        assert REQUIRED_TAGS.items() <= tags.items(), f"{logical_id}: {tags}"


def test_stack_has_no_forbidden_or_lambda_backed_resources(template: Template) -> None:
    types = {resource["Type"] for resource in template.to_json()["Resources"].values()}
    assert not types & FORBIDDEN_TYPES
    assert "AWS::Lambda::Function" not in types
    assert "AWS::EC2::VPC" not in types


def test_stack_has_no_github_oidc_resources(template: Template) -> None:
    types = {resource["Type"] for resource in template.to_json()["Resources"].values()}
    assert "AWS::IAM::OIDCProvider" not in types


def test_teardown_operates_in_the_same_region_as_the_stacks() -> None:
    assert teardown.REGION == REGION


@pytest.mark.parametrize(
    "overrides",
    [
        {"budget_email": "invalido"},
        {"budget_email": "a b@example.invalid"},
        {"kill_switch_users": ("usuario invalido",)},
    ],
)
def test_config_rejects_invalid_values(overrides: dict[str, Any]) -> None:
    base: dict[str, Any] = {"budget_email": EMAIL}
    with pytest.raises(ValueError, match=r"deve|inválidos"):
        FoundationConfig(**{**base, **overrides})


def test_load_config_refuses_to_run_without_required_variables() -> None:
    with pytest.raises(SystemExit, match=ENV_BUDGET_EMAIL):
        load_config({})


def test_load_config_parses_optional_users() -> None:
    config = load_config(
        {
            ENV_BUDGET_EMAIL: EMAIL,
            ENV_KILL_SWITCH_USERS: " ana , bia,",
        }
    )
    assert config.kill_switch_users == ("ana", "bia")


def test_app_exposes_foundation_stack_with_the_planned_id() -> None:
    app = build_app({ENV_BUDGET_EMAIL: EMAIL})
    stack = app.node.find_child("FoundationStack")
    assert isinstance(stack, FoundationStack)
    assert stack.stack_name == "chuva-bh-foundation"
    assert stack.region == "us-east-2"
