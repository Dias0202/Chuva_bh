"""Testes de PSI, qualidade, drift, ttl_sweeper e da ObservabilityStack."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import boto3
import numpy as np
import pandas as pd
import pytest
import ttl_sweeper
from aws_cdk import App, Environment
from aws_cdk.assertions import Template
from moto import mock_aws
from stacks.bundling import SKIP_BUNDLING_CONTEXT
from stacks.common import REGION
from stacks.foundation_stack import FoundationConfig, FoundationStack
from stacks.ml_stack import MlStack
from stacks.observability_stack import ObservabilityStack
from stacks.serving_stack import ServingStack

from ingestion.columns import OBSERVATIONS, Row
from ingestion.lake import months_between
from ingestion.storage import write_rows
from monitoring import drift, quality
from monitoring.psi import build_reference, drift_scores, psi, reference_bins
from serving.store import prediction_item

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_s3.client import S3Client

NOW = datetime(2026, 11, 10, 12)
TABLE = "forecasts"


def test_months_between_spans_years() -> None:
    assert months_between(datetime(2025, 11, 20), datetime(2026, 2, 1)) == [
        datetime(2025, 11, 1),
        datetime(2025, 12, 1),
        datetime(2026, 1, 1),
        datetime(2026, 2, 1),
    ]
    with pytest.raises(ValueError, match="início"):
        months_between(datetime(2026, 2, 1), datetime(2026, 1, 1))


def test_psi_is_zero_for_the_same_distribution_and_high_for_a_shift() -> None:
    rng = np.random.default_rng(0)
    reference = reference_bins(rng.normal(0, 1, 5000))

    same = psi(reference, rng.normal(0, 1, 2000))
    shifted = psi(reference, rng.normal(2, 1, 2000))

    assert same is not None
    assert same < 0.05
    assert shifted is not None
    assert shifted > 0.25
    assert psi(reference, [0.1, 0.2]) is None
    with pytest.raises(ValueError, match="poucas"):
        reference_bins([1.0, 2.0])


def test_drift_scores_skip_missing_features() -> None:
    rng = np.random.default_rng(1)
    frame = pd.DataFrame({"a": rng.normal(0, 1, 500), "b": rng.normal(0, 1, 500)})
    reference = build_reference(frame, ["a", "b"])

    scores = drift_scores(reference, frame[["a"]])

    assert set(scores) == {"a"}


def _observations(start: datetime, hours: int, point_id: str = "A521") -> list[Row]:
    rng = np.random.default_rng(3)
    rows: list[Row] = []
    for i in range(hours):
        rows.append(
            {
                "point_id": point_id,
                "observed_at": start + timedelta(hours=i),
                "source": "inmet_bulk",
                "precipitation_mm": float(rng.choice([0.0, 0.0, 0.0, 2.0])),
                "pressure_hpa": 920.0 + float(rng.normal()),
                "radiation_kj_m2": 0.0,
                "temperature_c": 22.0 + float(rng.normal()),
                "dew_point_c": 16.0,
                "relative_humidity_pct": 70.0 + float(rng.normal(0, 5)),
                "wind_direction_deg": 90.0,
                "wind_gust_ms": 3.0,
                "wind_speed_ms": 1.0,
            }
        )
    return rows


@pytest.fixture
def aws() -> Iterator[dict[str, Any]]:
    with mock_aws():
        s3: S3Client = boto3.client("s3", region_name="us-east-1")
        for bucket in ("curated", "artifacts"):
            s3.create_bucket(Bucket=bucket)
        dynamodb: DynamoDBClient = boto3.client("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "point_id", "KeyType": "HASH"},
                {"AttributeName": "issue_horizon", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "point_id", "AttributeType": "S"},
                {"AttributeName": "issue_horizon", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield {"s3": s3, "dynamodb": dynamodb, "cloudwatch": MagicMock()}


def test_quality_compares_model_and_persistence(aws: dict[str, Any]) -> None:
    start = NOW - timedelta(days=400)
    rows = _observations(start, 400 * 24 + 1)
    write_rows(aws["s3"], "curated", rows, OBSERVATIONS, "A521", "history.parquet")
    for hours_back in range(1, 7 * 24):
        issued = NOW - timedelta(hours=hours_back)
        for horizon in (1, 3, 6):
            aws["dynamodb"].put_item(
                TableName=TABLE, Item=prediction_item("A521", issued, horizon, 0.25, "1")
            )

    metrics = quality.run(
        NOW,
        s3=aws["s3"],
        dynamodb=aws["dynamodb"],
        cloudwatch=aws["cloudwatch"],
        bucket="curated",
        table=TABLE,
        points=("A521",),
    )

    assert set(metrics) == {7, 30}
    assert metrics[7]["Samples"] > 400
    assert metrics[7]["BrierDifference"] == pytest.approx(
        metrics[7]["BrierModel"] - metrics[7]["BrierBaseline"]
    )
    assert "PrAucModel" in metrics[30]
    sent = aws["cloudwatch"].put_metric_data.call_args.kwargs
    assert sent["Namespace"] == quality.METRIC_NAMESPACE


def test_quality_without_predictions_publishes_nothing(aws: dict[str, Any]) -> None:
    metrics = quality.run(
        NOW,
        s3=aws["s3"],
        dynamodb=aws["dynamodb"],
        cloudwatch=aws["cloudwatch"],
        bucket="curated",
        table=TABLE,
    )

    assert metrics == {}
    aws["cloudwatch"].put_metric_data.assert_not_called()


def test_drift_builds_and_caches_the_reference(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(drift, "REFERENCE_START", datetime(2026, 9, 1))
    monkeypatch.setattr(drift, "REFERENCE_END", datetime(2026, 10, 31, 23))
    rows = _observations(datetime(2026, 9, 1), 71 * 24)
    write_rows(aws["s3"], "curated", rows, OBSERVATIONS, "A521", "history.parquet")

    scores = drift.run(
        NOW,
        s3=aws["s3"],
        cloudwatch=aws["cloudwatch"],
        curated_bucket="curated",
        artifacts_bucket="artifacts",
        points=("A521",),
    )

    assert "relative_humidity_pct" in scores
    cached = json.loads(
        aws["s3"].get_object(Bucket="artifacts", Key=drift.REFERENCE_KEY)["Body"].read()
    )
    assert "precip_mm" in cached
    data = aws["cloudwatch"].put_metric_data.call_args.kwargs["MetricData"]
    assert data[0]["MetricName"] == "MaxPsi"


def test_drift_without_recent_observations_does_nothing(aws: dict[str, Any]) -> None:
    assert (
        drift.run(
            NOW,
            s3=aws["s3"],
            cloudwatch=aws["cloudwatch"],
            curated_bucket="curated",
            artifacts_bucket="artifacts",
        )
        == {}
    )


def test_ttl_sweeper_selects_only_expired_resources() -> None:
    resources = [
        ttl_sweeper.Tagged("arn:aws:sagemaker:r:1:endpoint/old", "2026-11-09"),
        ttl_sweeper.Tagged("arn:aws:sagemaker:r:1:endpoint/today", "2026-11-10"),
        ttl_sweeper.Tagged("arn:aws:sagemaker:r:1:endpoint/future", "2026-12-01"),
        ttl_sweeper.Tagged("arn:aws:sagemaker:r:1:model/bad", "amanhã"),
    ]

    due, invalid = ttl_sweeper.expired(resources, date(2026, 11, 10))

    assert due == ["arn:aws:sagemaker:r:1:endpoint/old", "arn:aws:sagemaker:r:1:endpoint/today"]
    assert [r.arn for r in invalid] == ["arn:aws:sagemaker:r:1:model/bad"]


def test_ttl_sweeper_deletes_schedules_by_name() -> None:
    client = MagicMock()

    ttl_sweeper.delete_schedules(
        client, ["arn:aws:sagemaker:r:1:monitoring-schedule/s1", "arn:aws:sagemaker:r:1:model/m"]
    )

    client.delete_monitoring_schedule.assert_called_once_with(MonitoringScheduleName="s1")


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
    serving = ServingStack(
        app,
        "Serving",
        env=env,
        curated_bucket=foundation.buckets["curated"],
        sagemaker_role=ml.role,
    )
    stack = ObservabilityStack(
        app,
        "Observability",
        env=env,
        curated_bucket=foundation.buckets["curated"],
        artifacts_bucket=foundation.buckets["artifacts"],
        table=serving.table,
        api=serving.api,
        alert_email="a@example.com",
    )
    return Template.from_stack(stack)


def test_alarms_notify_by_email(template: Template) -> None:
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    names = {cast(dict[str, Any], a)["Properties"]["AlarmName"] for a in alarms.values()}

    assert names == {
        "chuva-bh-IngestionStopped",
        "chuva-bh-ModelWorseThanBaseline",
        "chuva-bh-DriftHigh",
        "chuva-bh-ApiErrors",
    }
    for alarm in alarms.values():
        assert cast(dict[str, Any], alarm)["Properties"]["AlarmActions"]
    template.has_resource_properties("AWS::SNS::Subscription", {"Protocol": "email"})
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {"AlarmName": "chuva-bh-IngestionStopped", "TreatMissingData": "breaching"},
    )


def test_retrain_rule_starts_the_pipeline_with_manual_approval(template: Template) -> None:
    rule = cast(dict[str, Any], next(iter(template.find_resources("AWS::Events::Rule").values())))
    target = rule["Properties"]["Targets"][0]

    assert "pipeline/chuva-bh-training" in json.dumps(target["Arn"])
    parameters = target["SageMakerPipelineParameters"]["PipelineParameterList"]
    assert parameters == [{"Name": "ModelApprovalStatus", "Value": "PendingManualApproval"}]


def test_dashboard_and_schedules_exist(template: Template) -> None:
    template.resource_count_is("AWS::CloudWatch::Dashboard", 1)
    template.resource_count_is("AWS::Scheduler::Schedule", 2)
    template.all_resources_properties("AWS::Logs::LogGroup", {"RetentionInDays": 14})
