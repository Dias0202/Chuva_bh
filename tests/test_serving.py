"""Testes do scoring, da API e do deployer, com AWS simulada."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import boto3
import numpy as np
import pandas as pd
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from features.build import FEATURE_SETS
from ingestion.columns import FORECASTS, OBSERVATIONS, Row
from ingestion.storage import write_rows
from serving import api, deployer, score
from serving.store import (
    NO_MODEL,
    ModelConfig,
    parse_model_config,
    prediction_item,
    sort_key,
)

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_s3.client import S3Client
    from types_boto3_ssm.client import SSMClient

BUCKET = "curated"
TABLE = "forecasts"
PARAMETER = "/chuva-bh/serving/model"
ISSUED = datetime(2026, 11, 10, 12)
CONFIG = ModelConfig("chuva-bh-rain", "arn:pkg/1", "1", "full")


def test_model_config_round_trip_and_no_model() -> None:
    assert parse_model_config(CONFIG.to_json()) == CONFIG
    assert parse_model_config(json.dumps(NO_MODEL)) is None
    with pytest.raises(ValueError, match="incompleta"):
        parse_model_config('{"endpoint_name": "x"}')


def test_prediction_item_has_ttl_and_rejects_invalid_probability() -> None:
    item = prediction_item("A521", ISSUED, 3, 0.25, "1")

    assert item["issue_horizon"]["S"] == sort_key(ISSUED, 3) == "2026-11-10T12:00#03"
    assert item["valid_at"]["S"] == "2026-11-10T15:00"
    assert int(item["expires_at"]["N"]) > ISSUED.timestamp()
    with pytest.raises(ValueError, match="fora"):
        prediction_item("A521", ISSUED, 1, 1.5, "1")


@pytest.fixture
def aws() -> Iterator[dict[str, Any]]:
    with mock_aws():
        s3: S3Client = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
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
        ssm: SSMClient = boto3.client("ssm", region_name="us-east-1")
        ssm.put_parameter(Name=PARAMETER, Value=CONFIG.to_json(), Type="String")
        yield {
            "s3": s3,
            "dynamodb": dynamodb,
            "ssm": ssm,
            "cloudwatch": boto3.client("cloudwatch", region_name="us-east-1"),
        }


def _seed(s3: S3Client, point_id: str = "A521", with_current_hour: bool = True) -> None:
    hours = 31 if with_current_hour else 30
    observations: list[Row] = [
        {
            "point_id": point_id,
            "observed_at": ISSUED - timedelta(hours=30) + timedelta(hours=i),
            "source": "inmet_api",
            "precipitation_mm": 0.0,
            "pressure_hpa": 920.0,
            "radiation_kj_m2": 0.0,
            "temperature_c": 22.0,
            "dew_point_c": 16.0,
            "relative_humidity_pct": 70.0,
            "wind_direction_deg": 90.0,
            "wind_gust_ms": 3.0,
            "wind_speed_ms": 1.0,
        }
        for i in range(hours)
    ]
    write_rows(s3, BUCKET, observations, OBSERVATIONS, point_id, "api.parquet")
    forecasts: list[Row] = [
        {
            "point_id": point_id,
            "valid_at": ISSUED + timedelta(hours=i),
            "issued_at": ISSUED,
            "source": "open_meteo_forecast",
            "precipitation_mm": 1.0,
            "cape_j_kg": 100.0,
            "cloud_cover_pct": 50.0,
            "relative_humidity_pct": 80.0,
        }
        for i in range(10)
    ]
    write_rows(s3, BUCKET, forecasts, FORECASTS, point_id, f"issued-{ISSUED:%Y%m%dT%H}.parquet")


def _runtime(body: bytes = b"0.1\n0.2\n0.3") -> MagicMock:
    runtime = MagicMock()
    runtime.invoke_endpoint.return_value = {"Body": io.BytesIO(body)}
    return runtime


def _run(aws: dict[str, Any], runtime: MagicMock, points: tuple[str, ...]) -> score.ScoreResult:
    return score.run(
        ISSUED,
        s3=aws["s3"],
        runtime=runtime,
        dynamodb=aws["dynamodb"],
        cloudwatch=aws["cloudwatch"],
        ssm=aws["ssm"],
        bucket=BUCKET,
        table=TABLE,
        parameter=PARAMETER,
        points=points,
    )


def test_score_writes_one_item_per_horizon(aws: dict[str, Any]) -> None:
    _seed(aws["s3"])
    runtime = _runtime()

    result = _run(aws, runtime, ("A521",))

    assert result.scored == ("A521",)
    body = runtime.invoke_endpoint.call_args.kwargs["Body"].decode()
    assert len(body.splitlines()) == 3
    assert body.splitlines()[0].split(",")[0] == "1.0"
    items = aws["dynamodb"].scan(TableName=TABLE)["Items"]
    assert sorted(i["horizon_h"]["N"] for i in items) == ["1", "3", "6"]


def test_score_twice_overwrites_the_same_items(aws: dict[str, Any]) -> None:
    _seed(aws["s3"])
    _run(aws, _runtime(), ("A521",))
    _run(aws, _runtime(b"0.4,0.5,0.6"), ("A521",))

    items = aws["dynamodb"].scan(TableName=TABLE)["Items"]
    assert len(items) == 3
    assert sorted(float(i["probability"]["N"]) for i in items) == [0.4, 0.5, 0.6]


def test_score_skips_points_without_the_current_observation(aws: dict[str, Any]) -> None:
    _seed(aws["s3"], with_current_hour=False)
    runtime = _runtime()

    result = _run(aws, runtime, ("A521", "A535"))

    assert result.skipped == ("A521", "A535")
    runtime.invoke_endpoint.assert_not_called()


def test_score_without_an_approved_model_does_nothing(aws: dict[str, Any]) -> None:
    aws["ssm"].put_parameter(Name=PARAMETER, Value=json.dumps(NO_MODEL), Overwrite=True)
    runtime = _runtime()

    assert _run(aws, runtime, ("A521",)).skipped == ("A521",)
    runtime.invoke_endpoint.assert_not_called()


def test_score_raises_after_processing_when_the_endpoint_fails(aws: dict[str, Any]) -> None:
    _seed(aws["s3"])
    runtime = _runtime(b"0.1")

    with pytest.raises(score.ScoringError, match="A521"):
        _run(aws, runtime, ("A521",))


def test_to_csv_leaves_missing_values_empty() -> None:
    rows = pd.DataFrame({column: [np.nan] for column in FEATURE_SETS["obs_only"]})
    rows["horizon_h"] = 1

    line = score.to_csv(rows, "obs_only")

    assert line.startswith("1.0,")
    assert line.count(",") == len(FEATURE_SETS["obs_only"]) - 1


def _event(route: str, query: dict[str, str] | None = None) -> dict[str, Any]:
    return {"routeKey": route, "queryStringParameters": query}


def test_api_lists_points(aws: dict[str, Any]) -> None:
    response = api.route(_event("GET /points"), aws["dynamodb"], TABLE)

    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert {p["id"] for p in body["points"]} == {"A521", "A572", "A535", "A569"}


def test_api_returns_the_latest_issuance(aws: dict[str, Any]) -> None:
    for issued, value in ((ISSUED - timedelta(hours=1), 0.9), (ISSUED, 0.2)):
        for horizon in (1, 3, 6):
            aws["dynamodb"].put_item(
                TableName=TABLE, Item=prediction_item("A521", issued, horizon, value, "7")
            )

    response = api.route(_event("GET /forecast", {"point": "A521"}), aws["dynamodb"], TABLE)

    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["issued_at"] == "2026-11-10T12:00"
    assert [f["horizon_h"] for f in body["forecasts"]] == [1, 3, 6]
    assert {f["probability"] for f in body["forecasts"]} == {0.2}
    assert "Defesa Civil" in body["disclaimer"]


@pytest.mark.parametrize(
    "query",
    [None, {"point": "a521"}, {"point": "Z999"}, {"point": "A521", "extra": "1"}],
)
def test_api_rejects_invalid_queries(aws: dict[str, Any], query: dict[str, str] | None) -> None:
    response = api.route(_event("GET /forecast", query), aws["dynamodb"], TABLE)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["details"]


def test_api_returns_404_without_data_or_route(aws: dict[str, Any]) -> None:
    assert (
        api.route(_event("GET /forecast", {"point": "A535"}), aws["dynamodb"], TABLE)["statusCode"]
        == 404
    )
    assert api.route(_event("POST /forecast"), aws["dynamodb"], TABLE)["statusCode"] == 404


def test_api_handler_hides_backend_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    failing = MagicMock()
    failing.query.side_effect = ClientError({"Error": {"Code": "Throttling"}}, "Query")
    monkeypatch.setattr(api, "_dynamodb", lambda: failing)
    monkeypatch.setenv("TABLE_NAME", TABLE)

    response = api.handler(_event("GET /forecast", {"point": "A521"}), None)

    assert response["statusCode"] == 503


def _sagemaker(status: str = "Approved", group: str = "chuva-bh-rain") -> MagicMock:
    client = MagicMock()
    client.describe_model_package.return_value = {
        "ModelPackageGroupName": group,
        "ModelApprovalStatus": status,
        "ModelPackageVersion": 3,
        "CustomerMetadataProperties": {"feature_set": "obs_only"},
    }
    client.describe_endpoint.side_effect = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "Could not find endpoint"}},
        "DescribeEndpoint",
    )
    return client


def _deploy(aws: dict[str, Any], client: MagicMock) -> ModelConfig | None:
    return deployer.deploy(
        {"detail": {"ModelPackageArn": "arn:pkg/3"}},
        sagemaker=client,
        ssm=aws["ssm"],
        role_arn="arn:role",
        endpoint_name="chuva-bh-rain",
        parameter=PARAMETER,
        group="chuva-bh-rain",
    )


def test_deployer_creates_a_serverless_endpoint_and_updates_the_parameter(
    aws: dict[str, Any],
) -> None:
    client = _sagemaker()

    config = _deploy(aws, client)

    assert config is not None
    assert config.feature_set == "obs_only"
    variant = client.create_endpoint_config.call_args.kwargs["ProductionVariants"][0]
    assert variant["ServerlessConfig"]["MaxConcurrency"] == 2
    client.create_endpoint.assert_called_once()
    stored = aws["ssm"].get_parameter(Name=PARAMETER)["Parameter"]["Value"]
    assert parse_model_config(stored) == config


def test_deployer_updates_an_existing_endpoint_and_tolerates_replays(aws: dict[str, Any]) -> None:
    client = _sagemaker()
    client.describe_endpoint.side_effect = None
    client.describe_endpoint.return_value = {"EndpointConfigName": "chuva-bh-rain-v2"}
    client.create_model.side_effect = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "Model already exists"}},
        "CreateModel",
    )

    _deploy(aws, client)

    client.update_endpoint.assert_called_once_with(
        EndpointName="chuva-bh-rain", EndpointConfigName="chuva-bh-rain-v3"
    )


@pytest.mark.parametrize(
    ("status", "group"), [("PendingManualApproval", "chuva-bh-rain"), ("Approved", "outro")]
)
def test_deployer_ignores_unapproved_or_foreign_packages(
    aws: dict[str, Any], status: str, group: str
) -> None:
    client = _sagemaker(status, group)

    assert _deploy(aws, client) is None
    client.create_model.assert_not_called()


def test_deployer_rejects_events_without_package() -> None:
    with pytest.raises(ValueError, match="ModelPackageArn"):
        deployer.deploy(
            {},
            sagemaker=MagicMock(),
            ssm=MagicMock(),
            role_arn="r",
            endpoint_name="e",
            parameter="p",
            group="g",
        )
