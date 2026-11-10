"""Testes da ServingStack e do empacotamento com uv."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
from aws_cdk import App, Environment
from aws_cdk.assertions import Match, Template
from stacks.bundling import SKIP_BUNDLING_CONTEXT, X86_64, UvLocalBundling
from stacks.common import REGION
from stacks.foundation_stack import FoundationConfig, FoundationStack
from stacks.ml_stack import MlStack
from stacks.serving_stack import API_RATE_LIMIT, ServingStack


@pytest.fixture(scope="module")
def template() -> Template:
    app = App(context=SKIP_BUNDLING_CONTEXT)
    env = Environment(account="123456789012", region=REGION)
    foundation = FoundationStack(
        app, "Foundation", env=env, config=FoundationConfig(budget_email="a@example.com")
    )
    ml = MlStack(
        app,
        "Ml",
        env=env,
        curated_bucket=foundation.buckets["curated"],
        artifacts_bucket=foundation.buckets["artifacts"],
        key=foundation.key,
    )
    stack = ServingStack(
        app,
        "Serving",
        env=env,
        curated_bucket=foundation.buckets["curated"],
        sagemaker_role=ml.role,
    )
    return Template.from_stack(stack)


def _statements(template: Template) -> list[dict[str, Any]]:
    return [
        statement
        for policy in template.find_resources("AWS::IAM::Policy").values()
        for statement in cast(dict[str, Any], policy)["Properties"]["PolicyDocument"]["Statement"]
    ]


def test_table_is_on_demand_with_ttl(template: Template) -> None:
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "BillingMode": "PAY_PER_REQUEST",
            "TimeToLiveSpecification": {"AttributeName": "expires_at", "Enabled": True},
        },
    )


def test_api_routes_require_iam_and_are_throttled(template: Template) -> None:
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    keys = {cast(dict[str, Any], r)["Properties"]["RouteKey"] for r in routes.values()}

    assert keys == {"GET /points", "GET /forecast"}
    for route in routes.values():
        assert cast(dict[str, Any], route)["Properties"]["AuthorizationType"] == "AWS_IAM"
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {"DefaultRouteSettings": {"ThrottlingRateLimit": API_RATE_LIMIT}},
    )


def test_approval_rule_targets_the_model_group(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Events::Rule",
        {
            "EventPattern": {
                "detail": {
                    "ModelPackageGroupName": ["chuva-bh-rain"],
                    "ModelApprovalStatus": ["Approved"],
                }
            }
        },
    )


def test_scoring_runs_hourly_after_ingestion(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Scheduler::Schedule", {"ScheduleExpression": "cron(20 * * * ? *)"}
    )


def test_permissions_are_scoped_to_named_resources(template: Template) -> None:
    statements = _statements(template)
    invoke = next(s for s in statements if s.get("Sid") == "InvokeRainEndpoint")
    deploy = next(s for s in statements if s.get("Sid") == "DeployApprovedModel")

    assert "endpoint/chuva-bh-rain" in str(invoke["Resource"])
    assert "sagemaker:DeleteEndpoint" not in deploy["Action"]
    for statement in statements:
        actions = (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
        assert not [a for a in actions if a.endswith(":*")]


def test_every_log_group_keeps_fourteen_days(template: Template) -> None:
    groups = template.find_resources("AWS::Logs::LogGroup")

    assert len(groups) == 3
    template.all_resources_properties("AWS::Logs::LogGroup", {"RetentionInDays": 14})


def test_phase_tag(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"Tags": Match.array_with([Match.object_like({"Key": "Phase", "Value": "3"})])},
    )


def test_uv_bundling_installs_for_linux_and_copies_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "src"
    (source / "serving" / "__pycache__").mkdir(parents=True)
    (source / "serving" / "api.py").write_text("x = 1\n", encoding="utf-8")
    (source / "serving" / "__pycache__" / "api.pyc").write_bytes(b"0")
    calls: list[list[str]] = []
    monkeypatch.setattr(shutil, "which", lambda _: "uv")
    monkeypatch.setattr(subprocess, "run", lambda args, check: calls.append(args))
    output = tmp_path / "out"

    bundler = UvLocalBundling(source, ["serving"], ["pydantic==2.14.3"], X86_64)
    done = bundler.try_bundle(str(output), image=cast(Any, None))

    assert done is True
    assert calls[0][calls[0].index("--python-platform") + 1] == X86_64
    assert (output / "serving" / "api.py").exists()
    assert not (output / "serving" / "__pycache__").exists()


def test_uv_bundling_falls_back_without_uv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)

    assert UvLocalBundling(tmp_path, [], [], X86_64).try_bundle(str(tmp_path)) is False
