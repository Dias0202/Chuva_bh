"""Lambda horária de scoring: monta as features da hora, chama o endpoint e grava no DynamoDB."""

from __future__ import annotations

import io
import logging
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import boto3
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from botocore.exceptions import BotoCoreError, ClientError

from features.build import FEATURE_SETS, HORIZONS, build_dataset
from ingestion.handler import parse_issued_at
from ingestion.points import POINT_IDS
from serving.store import ModelConfig, parse_model_config, prediction_item

if TYPE_CHECKING:
    from types_boto3_cloudwatch.client import CloudWatchClient
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_s3.client import S3Client
    from types_boto3_sagemaker_runtime.client import SageMakerRuntimeClient
    from types_boto3_ssm.client import SSMClient

logger = logging.getLogger()
logger.setLevel(logging.INFO)

METRIC_NAMESPACE: Final = "ChuvaBh/Serving"
LOOKBACK_HOURS: Final = 30
_SEPARATOR: Final = re.compile(r"[,\n]")


class ScoringError(RuntimeError):
    """Algum ponto com observação disponível não pôde ser pontuado."""


@dataclass(frozen=True)
class ScoreResult:
    scored: tuple[str, ...]
    skipped: tuple[str, ...]
    failed: tuple[str, ...]


def _partition(dataset: str, moment: datetime, point_id: str) -> str:
    return f"{dataset}/year={moment.year:04d}/month={moment.month:02d}/point_id={point_id}/"


def _read_objects(s3: S3Client, bucket: str, keys: Iterable[str]) -> pd.DataFrame:
    frames = []
    for key in keys:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        frames.append(pq.read_table(io.BytesIO(body)).to_pandas())  # type: ignore[no-untyped-call]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _list(s3: S3Client, bucket: str, prefix: str) -> list[str]:
    paginator = s3.get_paginator("list_objects_v2")
    return [
        item["Key"]
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
        for item in page.get("Contents", [])
    ]


def load_observations(
    s3: S3Client, bucket: str, point_id: str, issued_at: datetime
) -> pd.DataFrame:
    start = issued_at - timedelta(hours=LOOKBACK_HOURS)
    prefixes = {_partition("observations", m, point_id) for m in (start, issued_at)}
    keys = [k for prefix in sorted(prefixes) for k in _list(s3, bucket, prefix)]
    frame = _read_objects(s3, bucket, keys)
    if frame.empty:
        return frame
    frame["point_id"] = point_id
    window = (frame["observed_at"] >= start) & (frame["observed_at"] <= issued_at)
    return frame[window]


def load_forecast(s3: S3Client, bucket: str, point_id: str, issued_at: datetime) -> pd.DataFrame:
    name = f"issued-{issued_at:%Y%m%dT%H}.parquet"
    last = issued_at + timedelta(hours=max(HORIZONS))
    prefixes = sorted({_partition("forecasts", m, point_id) for m in (issued_at, last)})
    keys = [k for prefix in prefixes for k in _list(s3, bucket, prefix) if k.endswith(name)]
    frame = _read_objects(s3, bucket, keys)
    if not frame.empty:
        frame["point_id"] = point_id
    return frame


def feature_rows(
    observations: pd.DataFrame,
    forecast: pd.DataFrame,
    issued_at: datetime,
    feature_set: str,
) -> pd.DataFrame | None:
    """Linhas da hora de emissão, ou None se faltar a observação dessa hora."""
    if observations.empty:
        return None
    rows = build_dataset(observations, forecast if not forecast.empty else None, with_labels=False)
    rows = rows[rows["issued_at"] == pd.Timestamp(issued_at)]
    if rows.empty or rows["precip_mm"].isna().all():
        return None
    return rows[["horizon_h", *FEATURE_SETS[feature_set][1:]]].sort_values("horizon_h")


def to_csv(rows: pd.DataFrame, feature_set: str) -> str:
    """CSV sem cabeçalho, na ordem do treino. Valores ausentes ficam vazios."""
    columns = list(FEATURE_SETS[feature_set])
    lines = []
    for values in rows[columns].to_numpy(dtype=float):
        lines.append(",".join("" if np.isnan(v) else repr(float(v)) for v in values))
    return "\n".join(lines)


def parse_predictions(body: bytes, expected: int) -> list[float]:
    values = [float(v) for v in _SEPARATOR.split(body.decode("utf-8")) if v.strip()]
    if len(values) != expected:
        raise ValueError(f"esperava {expected} probabilidades e recebi {len(values)}")
    return values


def _score_point(
    point_id: str,
    issued_at: datetime,
    config: ModelConfig,
    s3: S3Client,
    runtime: SageMakerRuntimeClient,
    dynamodb: DynamoDBClient,
    bucket: str,
    table: str,
) -> bool:
    observations = load_observations(s3, bucket, point_id, issued_at)
    forecast = load_forecast(s3, bucket, point_id, issued_at)
    rows = feature_rows(observations, forecast, issued_at, config.feature_set)
    if rows is None:
        return False
    response = runtime.invoke_endpoint(
        EndpointName=config.endpoint_name,
        ContentType="text/csv",
        Accept="text/csv",
        Body=to_csv(rows, config.feature_set).encode("utf-8"),
    )
    probabilities = parse_predictions(response["Body"].read(), len(rows))
    for horizon, probability in zip(rows["horizon_h"].astype(int), probabilities, strict=True):
        dynamodb.put_item(
            TableName=table,
            Item=prediction_item(
                point_id, issued_at, int(horizon), probability, config.model_version
            ),
        )
    return True


def _publish(cloudwatch: CloudWatchClient, result: ScoreResult) -> None:
    cloudwatch.put_metric_data(
        Namespace=METRIC_NAMESPACE,
        MetricData=[
            {"MetricName": name, "Value": float(len(value)), "Unit": "Count"}
            for name, value in (
                ("PointsScored", result.scored),
                ("PointsSkipped", result.skipped),
                ("PointsFailed", result.failed),
            )
        ],
    )


def run(
    issued_at: datetime,
    *,
    s3: S3Client,
    runtime: SageMakerRuntimeClient,
    dynamodb: DynamoDBClient,
    cloudwatch: CloudWatchClient,
    ssm: SSMClient,
    bucket: str,
    table: str,
    parameter: str,
    points: Iterable[str] = POINT_IDS,
) -> ScoreResult:
    raw = ssm.get_parameter(Name=parameter)["Parameter"].get("Value", "{}")
    config = parse_model_config(raw)
    selected = list(points)
    if config is None:
        logger.info("Nenhum modelo aprovado ainda; scoring ignorado")
        result = ScoreResult((), tuple(selected), ())
        _publish(cloudwatch, result)
        return result

    scored: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    for point_id in selected:
        try:
            done = _score_point(point_id, issued_at, config, s3, runtime, dynamodb, bucket, table)
        except (ClientError, BotoCoreError, ValueError):
            logger.exception("Falha ao pontuar %s", point_id)
            failed.append(point_id)
            continue
        (scored if done else skipped).append(point_id)

    result = ScoreResult(tuple(scored), tuple(skipped), tuple(failed))
    _publish(cloudwatch, result)
    logger.info("Scoring %s: %s", issued_at.isoformat(), result)
    if failed:
        raise ScoringError(f"Pontos com falha: {', '.join(failed)}")
    return result


def handler(event: Mapping[str, Any], _context: object) -> dict[str, Any]:
    result = run(
        parse_issued_at(event),
        s3=boto3.client("s3"),
        runtime=boto3.client("sagemaker-runtime"),
        dynamodb=boto3.client("dynamodb"),
        cloudwatch=boto3.client("cloudwatch"),
        ssm=boto3.client("ssm"),
        bucket=os.environ["CURATED_BUCKET"],
        table=os.environ["TABLE_NAME"],
        parameter=os.environ["MODEL_PARAMETER"],
    )
    return {"scored": list(result.scored), "skipped": list(result.skipped)}
