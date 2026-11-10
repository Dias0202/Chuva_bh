"""Lambda horária: compara as previsões emitidas com a chuva observada.

A referência é a persistência calibrada no ano anterior à janela, com os mesmos dados
observados que o modelo usa. As métricas saem por janela de 7 e 30 dias.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import boto3
import numpy as np
import pandas as pd

from features.build import LABEL, build_dataset
from ingestion.handler import parse_issued_at
from ingestion.lake import read_range
from ingestion.points import POINT_IDS
from serving.store import sort_key
from training.baselines import PersistenceBaseline
from training.metrics import average_precision, brier_score, contingency

if TYPE_CHECKING:
    from types_boto3_cloudwatch.client import CloudWatchClient
    from types_boto3_cloudwatch.type_defs import MetricDatumTypeDef
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_s3.client import S3Client

logger = logging.getLogger()
logger.setLevel(logging.INFO)

METRIC_NAMESPACE: Final = "ChuvaBh/Quality"
WINDOWS_DAYS: Final = (7, 30)
CALIBRATION_DAYS: Final = 365
CSI_THRESHOLD: Final = 0.3
MIN_SAMPLES: Final = 24


def load_predictions(
    dynamodb: DynamoDBClient, table: str, point_id: str, start: datetime, end: datetime
) -> pd.DataFrame:
    paginator = dynamodb.get_paginator("query")
    rows = []
    for page in paginator.paginate(
        TableName=table,
        KeyConditionExpression="point_id = :p AND issue_horizon BETWEEN :a AND :b",
        ExpressionAttributeValues={
            ":p": {"S": point_id},
            ":a": {"S": sort_key(start, 0)},
            ":b": {"S": sort_key(end, 99)},
        },
    ):
        for item in page["Items"]:
            rows.append(
                {
                    "point_id": point_id,
                    "issued_at": pd.Timestamp(item["issued_at"]["S"]),
                    "horizon_h": int(item["horizon_h"]["N"]),
                    "probability": float(item["probability"]["N"]),
                }
            )
    return pd.DataFrame(rows)


def scored_rows(
    predictions: pd.DataFrame, observations: pd.DataFrame, calibration_end: datetime
) -> pd.DataFrame:
    """Junta previsão, rótulo observado e probabilidade da persistência calibrada."""
    if predictions.empty or observations.empty:
        return pd.DataFrame()
    rows = build_dataset(observations, None)
    keys = ["point_id", "issued_at", "horizon_h"]
    calibration = rows[rows["issued_at"] < pd.Timestamp(calibration_end)]
    joined = predictions.merge(rows[[*keys, "precip_mm", LABEL]], on=keys, how="inner")
    if joined.empty or calibration.empty:
        return pd.DataFrame()
    joined["baseline"] = PersistenceBaseline().fit(calibration).predict(joined)
    return joined


def window_metrics(rows: pd.DataFrame, end: datetime) -> dict[int, dict[str, float]]:
    metrics = {}
    for days in WINDOWS_DAYS:
        window = rows[rows["issued_at"] >= pd.Timestamp(end - timedelta(days=days))]
        if len(window) < MIN_SAMPLES:
            continue
        y = window[LABEL].to_numpy(dtype=float)
        model = window["probability"].to_numpy(dtype=float)
        baseline = window["baseline"].to_numpy(dtype=float)
        brier_model = brier_score(y, model)
        brier_baseline = brier_score(y, baseline)
        values = {
            "BrierModel": brier_model,
            "BrierBaseline": brier_baseline,
            "BrierDifference": brier_model - brier_baseline,
            "Samples": float(len(window)),
        }
        if days == max(WINDOWS_DAYS):
            ap = average_precision(y, model)
            values["PrAucModel"] = ap if not np.isnan(ap) else 0.0
            csi = contingency(y, model, CSI_THRESHOLD)["csi"]
            values["CsiModel"] = csi if not np.isnan(csi) else 0.0
        metrics[days] = values
    return metrics


def publish(cloudwatch: CloudWatchClient, metrics: Mapping[int, Mapping[str, float]]) -> None:
    data: list[MetricDatumTypeDef] = [
        {
            "MetricName": name,
            "Dimensions": [{"Name": "Window", "Value": f"{days}d"}],
            "Value": value,
        }
        for days, values in metrics.items()
        for name, value in values.items()
    ]
    if data:
        cloudwatch.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=data)


def run(
    now: datetime,
    *,
    s3: S3Client,
    dynamodb: DynamoDBClient,
    cloudwatch: CloudWatchClient,
    bucket: str,
    table: str,
    points: Iterable[str] = POINT_IDS,
) -> dict[int, dict[str, float]]:
    window_start = now - timedelta(days=max(WINDOWS_DAYS))
    calibration_start = window_start - timedelta(days=CALIBRATION_DAYS)
    frames = []
    for point_id in points:
        predictions = load_predictions(dynamodb, table, point_id, window_start, now)
        if predictions.empty:
            continue
        observations = read_range(s3, bucket, "observations", point_id, calibration_start, now)
        frames.append(scored_rows(predictions, observations, window_start))
    rows = pd.concat([f for f in frames if not f.empty]) if frames else pd.DataFrame()
    metrics = window_metrics(rows, now) if not rows.empty else {}
    publish(cloudwatch, metrics)
    logger.info("Qualidade em %s: %s", now.isoformat(), metrics or "sem amostras suficientes")
    return metrics


def handler(event: Mapping[str, Any], _context: object) -> dict[str, Any]:
    metrics = run(
        parse_issued_at(event),
        s3=boto3.client("s3"),
        dynamodb=boto3.client("dynamodb"),
        cloudwatch=boto3.client("cloudwatch"),
        bucket=os.environ["CURATED_BUCKET"],
        table=os.environ["TABLE_NAME"],
    )
    return {str(days): values for days, values in metrics.items()}
