"""Lambda diária: PSI das features observadas na última semana contra o período de treino.

A referência é calculada na primeira execução a partir do data lake e guardada no bucket
de artefatos. Para recalcular depois de um retreino, apague o objeto de referência.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import boto3
import pandas as pd
from botocore.exceptions import ClientError

from features.build import OBSERVATION_FEATURES, observation_features
from ingestion.handler import parse_issued_at
from ingestion.lake import read_range
from ingestion.points import POINT_IDS
from monitoring.psi import build_reference, drift_scores

if TYPE_CHECKING:
    from types_boto3_cloudwatch.client import CloudWatchClient
    from types_boto3_cloudwatch.type_defs import MetricDatumTypeDef
    from types_boto3_s3.client import S3Client

logger = logging.getLogger()
logger.setLevel(logging.INFO)

METRIC_NAMESPACE: Final = "ChuvaBh/Drift"
REFERENCE_KEY: Final = "monitoring/drift-reference.json"
# Mesmo período do treino padrão (ADR-009 e docs/evaluation.md).
REFERENCE_START: Final = datetime(2021, 3, 24)
REFERENCE_END: Final = datetime(2023, 12, 31, 23)
CURRENT_DAYS: Final = 7
# Só estas features viram métrica própria, para conter o custo; as demais ficam no log.
PUBLISHED_FEATURES: Final = (
    "precip_sum_3h_mm",
    "relative_humidity_pct",
    "pressure_tendency_3h_hpa",
)


def _features(
    s3: S3Client, bucket: str, points: Iterable[str], start: datetime, end: datetime
) -> pd.DataFrame:
    frames = []
    for point_id in points:
        observations = read_range(s3, bucket, "observations", point_id, start, end)
        if observations.empty:
            continue
        window = (observations["observed_at"] >= start) & (observations["observed_at"] <= end)
        if window.any():
            frames.append(observation_features(observations[window]))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def load_reference(
    s3: S3Client, curated_bucket: str, artifacts_bucket: str, points: Iterable[str]
) -> dict[str, Any]:
    try:
        body = s3.get_object(Bucket=artifacts_bucket, Key=REFERENCE_KEY)["Body"].read()
        reference: dict[str, Any] = json.loads(body)
        return reference
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") not in ("NoSuchKey", "404"):
            raise
    frame = _features(s3, curated_bucket, points, REFERENCE_START, REFERENCE_END)
    if frame.empty:
        raise ValueError("sem observações no período de referência")
    reference = build_reference(frame, OBSERVATION_FEATURES)
    s3.put_object(
        Bucket=artifacts_bucket,
        Key=REFERENCE_KEY,
        Body=json.dumps(reference).encode("utf-8"),
        ContentType="application/json",
    )
    return reference


def publish(cloudwatch: CloudWatchClient, scores: Mapping[str, float]) -> None:
    data: list[MetricDatumTypeDef] = [{"MetricName": "MaxPsi", "Value": max(scores.values())}]
    data += [
        {"MetricName": "Psi", "Dimensions": [{"Name": "Feature", "Value": f}], "Value": scores[f]}
        for f in PUBLISHED_FEATURES
        if f in scores
    ]
    cloudwatch.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=data)


def run(
    now: datetime,
    *,
    s3: S3Client,
    cloudwatch: CloudWatchClient,
    curated_bucket: str,
    artifacts_bucket: str,
    points: Iterable[str] = POINT_IDS,
) -> dict[str, float]:
    selected = list(points)
    current = _features(s3, curated_bucket, selected, now - timedelta(days=CURRENT_DAYS), now)
    if current.empty:
        logger.info("Sem observações na última semana; drift não calculado")
        return {}
    reference = load_reference(s3, curated_bucket, artifacts_bucket, selected)
    scores = drift_scores(reference, current)
    if scores:
        publish(cloudwatch, scores)
    logger.info("PSI por feature: %s", json.dumps(scores, sort_keys=True))
    return scores


def handler(event: Mapping[str, Any], _context: object) -> dict[str, Any]:
    return run(
        parse_issued_at(event),
        s3=boto3.client("s3"),
        cloudwatch=boto3.client("cloudwatch"),
        curated_bucket=os.environ["CURATED_BUCKET"],
        artifacts_bucket=os.environ["ARTIFACTS_BUCKET"],
    )
