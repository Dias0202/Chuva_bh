"""Lambda horária: publica as previsões mais recentes em data/forecasts.json no site.

O JSON é público e estático: o site mostra as previsões sem chamar nenhuma API.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import boto3

from ingestion.points import POINTS
from serving.api import DISCLAIMER, latest_forecast

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_s3.client import S3Client

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SNAPSHOT_KEY: Final = "data/forecasts.json"
CACHE_SECONDS: Final = 300


def build_snapshot(dynamodb: DynamoDBClient, table: str, generated_at: datetime) -> dict[str, Any]:
    points = []
    for point in POINTS:
        forecast = latest_forecast(dynamodb, table, point.point_id)
        points.append(
            {
                "id": point.point_id,
                "name": point.name,
                "latitude": point.latitude,
                "longitude": point.longitude,
                "forecast": forecast.model_dump() if forecast else None,
            }
        )
    return {
        "generated_at": generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "disclaimer": DISCLAIMER,
        "points": points,
    }


def publish(s3: S3Client, bucket: str, snapshot: Mapping[str, Any]) -> None:
    s3.put_object(
        Bucket=bucket,
        Key=SNAPSHOT_KEY,
        Body=json.dumps(snapshot, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
        CacheControl=f"max-age={CACHE_SECONDS}",
    )


def handler(_event: Mapping[str, Any], _context: object) -> dict[str, Any]:
    snapshot = build_snapshot(boto3.client("dynamodb"), os.environ["TABLE_NAME"], datetime.now(UTC))
    publish(boto3.client("s3"), os.environ["SITE_BUCKET"], snapshot)
    available = [p["id"] for p in snapshot["points"] if p["forecast"]]
    logger.info("Previsões publicadas para %s", available or "nenhum ponto")
    return {"published": available}
