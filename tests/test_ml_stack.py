"""Testes da MlStack e da definição do SageMaker Pipeline."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, cast

import boto3
import pytest
from aws_cdk import App, Environment
from aws_cdk.assertions import Template
from moto import mock_aws
from stacks.common import REGION
from stacks.foundation_stack import FoundationConfig, FoundationStack
from stacks.ml_stack import MlStack

from training.names import MODEL_PACKAGE_GROUP


def _template(isolation: bool) -> Template:
    app = App()
    env = Environment(account="123456789012", region=REGION)
    foundation = FoundationStack(
        app, "Foundation", env=env, config=FoundationConfig(budget_email="a@example.com")
    )
    stack = MlStack(
        app,
        "Ml",
        env=env,
        curated_bucket=foundation.buckets["curated"],
        artifacts_bucket=foundation.buckets["artifacts"],
        key=foundation.key,
        isolation=isolation,
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template() -> Template:
    return _template(isolation=False)


def _actions(template: Template) -> list[str]:
    actions: list[str] = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in cast(dict[str, Any], policy)["Properties"]["PolicyDocument"]["Statement"]:
            value = statement["Action"]
            actions += value if isinstance(value, list) else [value]
    return actions


def test_model_package_group_exists(template: Template) -> None:
    template.has_resource_properties(
        "AWS::SageMaker::ModelPackageGroup", {"ModelPackageGroupName": MODEL_PACKAGE_GROUP}
    )


def test_role_cannot_create_endpoints_or_use_wildcards(template: Template) -> None:
    actions = _actions(template)

    assert "sagemaker:CreateTrainingJob" in actions
    assert not [a for a in actions if a.endswith(":*") or a == "*"]
    assert not [a for a in actions if "Endpoint" in a]
    assert not [a for a in actions if a.startswith("s3:Delete")]


def test_default_deployment_has_no_network(template: Template) -> None:
    template.resource_count_is("AWS::EC2::VPC", 0)


def test_isolation_creates_a_vpc_without_nat_and_with_s3_gateway() -> None:
    isolated = _template(isolation=True)

    isolated.resource_count_is("AWS::EC2::VPC", 1)
    isolated.resource_count_is("AWS::EC2::NatGateway", 0)
    isolated.resource_count_is("AWS::EC2::InternetGateway", 0)
    isolated.has_resource_properties("AWS::EC2::VPCEndpoint", {"VpcEndpointType": "Gateway"})


@pytest.fixture
def definition() -> Iterator[dict[str, Any]]:
    from sagemaker.workflow.pipeline_context import PipelineSession

    from training.pipeline import PipelineSettings, build_pipeline

    with mock_aws():
        session = boto3.Session(region_name=REGION)
        session.client("s3").create_bucket(
            Bucket="artifacts", CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        pipeline = build_pipeline(
            PipelineSettings(
                role_arn="arn:aws:iam::123456789012:role/sagemaker",
                artifacts_bucket="artifacts",
                curated_bucket="curated",
                kms_key_arn="arn:aws:kms:us-east-2:123456789012:key/abc",
                region=REGION,
            ),
            PipelineSession(boto_session=session, default_bucket="artifacts"),
        )
        yield json.loads(pipeline.definition())


def test_pipeline_has_the_planned_steps(definition: dict[str, Any]) -> None:
    steps = {step["Name"]: step for step in definition["Steps"]}

    assert list(steps) == ["Preprocess", "Train", "Evaluate", "BeatsBaseline"]
    condition = steps["BeatsBaseline"]["Arguments"]
    assert [s["Name"] for s in condition["IfSteps"]][-1].startswith("Register")
    assert condition["ElseSteps"][0]["Type"] == "Fail"


def test_training_uses_spot_with_bounded_runtime(definition: dict[str, Any]) -> None:
    train = next(s for s in definition["Steps"] if s["Name"] == "Train")["Arguments"]

    assert train["EnableManagedSpotTraining"] is True
    assert train["StoppingCondition"]["MaxRuntimeInSeconds"] <= 3600
    assert train["StoppingCondition"]["MaxWaitTimeInSeconds"] <= 3600
    assert "CheckpointConfig" in train
