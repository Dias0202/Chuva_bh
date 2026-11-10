"""Testes da IngestionStack sobre o template sintetizado."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from aws_cdk import App, Environment
from aws_cdk.assertions import Match, Template
from stacks.common import REGION
from stacks.foundation_stack import FoundationConfig, FoundationStack
from stacks.ingestion_stack import ATHENA_BYTES_LIMIT, PANDAS_LAYER_ARN, IngestionStack

from ingestion.columns import FORECASTS, OBSERVATIONS
from ingestion.points import POINT_IDS


@pytest.fixture(scope="module")
def template() -> Template:
    app = App()
    env = Environment(account="123456789012", region=REGION)
    foundation = FoundationStack(
        app, "Foundation", env=env, config=FoundationConfig(budget_email="a@example.com")
    )
    stack = IngestionStack(
        app,
        "Ingestion",
        env=env,
        raw_bucket=foundation.buckets["raw"],
        curated_bucket=foundation.buckets["curated"],
        artifacts_bucket=foundation.buckets["artifacts"],
        key=foundation.key,
    )
    return Template.from_stack(stack)


def _statements(template: Template) -> list[dict[str, Any]]:
    policies = template.find_resources("AWS::IAM::Policy")
    return [
        statement
        for policy in policies.values()
        for statement in cast(dict[str, Any], policy)["Properties"]["PolicyDocument"]["Statement"]
    ]


def test_function_uses_the_layer_and_has_a_dead_letter_queue(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Runtime": "python3.12",
            "Handler": "ingestion.handler.handler",
            "Layers": [PANDAS_LAYER_ARN],
            "DeadLetterConfig": {"TargetArn": Match.any_value()},
        },
    )
    template.has_resource_properties("AWS::Lambda::EventInvokeConfig", {"MaximumRetryAttempts": 2})


def test_log_group_keeps_fourteen_days(template: Template) -> None:
    template.has_resource_properties("AWS::Logs::LogGroup", {"RetentionInDays": 14})


def test_schedule_runs_hourly_and_identifies_the_execution(template: Template) -> None:
    schedules = template.find_resources("AWS::Scheduler::Schedule")
    properties = cast(dict[str, Any], next(iter(schedules.values())))["Properties"]

    assert properties["ScheduleExpression"] == "cron(5 * * * ? *)"
    assert json.loads(properties["Target"]["Input"]) == {
        "scheduled_time": "<aws.scheduler.scheduled-time>"
    }
    assert properties["Target"]["DeadLetterConfig"]["Arn"]


def test_function_cannot_delete_objects(template: Template) -> None:
    actions = [
        action
        for statement in _statements(template)
        for action in (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
    ]

    assert not [action for action in actions if action.startswith("s3:Delete")]
    assert "s3:*" not in actions


def test_metric_permission_is_limited_to_the_namespace(template: Template) -> None:
    statement = next(s for s in _statements(template) if s.get("Sid") == "PublishIngestionMetrics")

    assert statement["Condition"] == {"StringEquals": {"cloudwatch:namespace": "ChuvaBh/Ingestion"}}


@pytest.mark.parametrize("dataset", [OBSERVATIONS, FORECASTS], ids=lambda d: d.name)
def test_tables_match_the_dataset_and_project_partitions(template: Template, dataset: Any) -> None:
    tables = template.find_resources("AWS::Glue::Table")
    table_input = next(
        cast(dict[str, Any], table)["Properties"]["TableInput"]
        for table in tables.values()
        if cast(dict[str, Any], table)["Properties"]["TableInput"]["Name"] == dataset.name
    )

    columns = table_input["StorageDescriptor"]["Columns"]
    assert [c["Name"] for c in columns] == [c.name for c in dataset.columns]
    assert [c["Name"] for c in table_input["PartitionKeys"]] == ["year", "month", "point_id"]
    assert table_input["Parameters"]["projection.enabled"] == "true"
    assert table_input["Parameters"]["projection.point_id.values"] == ",".join(POINT_IDS)


def test_no_recurring_crawler(template: Template) -> None:
    template.resource_count_is("AWS::Glue::Crawler", 0)


def test_workgroup_limits_scanned_bytes_and_encrypts_results(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Athena::WorkGroup",
        {
            "WorkGroupConfiguration": {
                "EnforceWorkGroupConfiguration": True,
                "BytesScannedCutoffPerQuery": ATHENA_BYTES_LIMIT,
                "ResultConfiguration": {"EncryptionConfiguration": {"EncryptionOption": "SSE_KMS"}},
            }
        },
    )


def test_every_taggable_resource_has_phase_one_tags(template: Template) -> None:
    for resource_type in ("AWS::Lambda::Function", "AWS::SQS::Queue", "AWS::Logs::LogGroup"):
        template.has_resource_properties(
            resource_type,
            {"Tags": Match.array_with([Match.object_like({"Key": "Phase", "Value": "1"})])},
        )
