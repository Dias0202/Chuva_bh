"""Lambda horária: grava a previsão operacional em JSON bruto e Parquet curado."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from ingestion.columns import FORECASTS
from ingestion.http import Fetch, FetchError, get_bytes
from ingestion.open_meteo import OpenMeteoFormatError, fetch_forecast
from ingestion.points import POINTS, Point
from ingestion.quality import QualityReport, prepare
from ingestion.storage import write_rows

if TYPE_CHECKING:
    from types_boto3_cloudwatch.client import CloudWatchClient
    from types_boto3_cloudwatch.type_defs import MetricDatumTypeDef
    from types_boto3_s3.client import S3Client

logger = logging.getLogger()
logger.setLevel(logging.INFO)

METRIC_NAMESPACE: Final = "ChuvaBh/Ingestion"
RAW_PREFIX: Final = "open_meteo/forecast"

_RECOVERABLE: Final = (FetchError, OpenMeteoFormatError, ClientError, BotoCoreError)


class IngestionError(RuntimeError):
    """Algum ponto falhou. A Lambda deve repetir a execução, que é idempotente."""


@dataclass(frozen=True)
class RunResult:
    issued_at: datetime
    report: QualityReport
    failed_points: tuple[str, ...]


def parse_issued_at(event: Mapping[str, Any]) -> datetime:
    """Usa o horário agendado, truncado na hora, como identidade da execução."""
    raw = event.get("scheduled_time")
    if not isinstance(raw, str):
        raise ValueError("evento sem scheduled_time")
    moment = datetime.fromisoformat(raw)
    if moment.tzinfo is None:
        raise ValueError("scheduled_time precisa informar o fuso")
    return moment.astimezone(UTC).replace(tzinfo=None, minute=0, second=0, microsecond=0)


def _ingest_point(
    point: Point,
    issued_at: datetime,
    s3: S3Client,
    raw_bucket: str,
    curated_bucket: str,
    fetch: Fetch,
) -> QualityReport:
    stamp = issued_at.strftime("%Y%m%dT%H")
    body, rows = fetch_forecast(point, fetch, issued_at)
    s3.put_object(
        Bucket=raw_bucket,
        Key=f"{RAW_PREFIX}/date={issued_at:%Y-%m-%d}/point_id={point.point_id}/{stamp}.json",
        Body=body,
        ContentType="application/json",
    )
    clean, report = prepare(rows, FORECASTS)
    write_rows(s3, curated_bucket, clean, FORECASTS, point.point_id, f"issued-{stamp}.parquet")
    return report


def _sum_reports(reports: Iterable[QualityReport]) -> QualityReport:
    items = list(reports)
    return QualityReport(
        rows=sum(r.rows for r in items),
        null_values=sum(r.null_values for r in items),
        out_of_range=sum(r.out_of_range for r in items),
        duplicate_keys=sum(r.duplicate_keys for r in items),
        temporal_gaps=sum(r.temporal_gaps for r in items),
    )


def publish_metrics(cloudwatch: CloudWatchClient, result: RunResult) -> None:
    values = {
        "RowsWritten": result.report.rows,
        "NullValues": result.report.null_values,
        "OutOfRange": result.report.out_of_range,
        "DuplicateKeys": result.report.duplicate_keys,
        "TemporalGaps": result.report.temporal_gaps,
        "PointsFailed": len(result.failed_points),
    }
    metric_data: list[MetricDatumTypeDef] = [
        {
            "MetricName": name,
            "Dimensions": [{"Name": "Dataset", "Value": FORECASTS.name}],
            "Value": float(value),
            "Unit": "Count",
        }
        for name, value in values.items()
    ]
    cloudwatch.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=metric_data)


def run(
    issued_at: datetime,
    *,
    s3: S3Client,
    cloudwatch: CloudWatchClient,
    raw_bucket: str,
    curated_bucket: str,
    fetch: Fetch,
    points: Iterable[Point] = POINTS,
) -> RunResult:
    """Processa todos os pontos, para que a falha de um não impeça os demais."""
    reports: list[QualityReport] = []
    failed: list[str] = []
    for point in points:
        try:
            reports.append(_ingest_point(point, issued_at, s3, raw_bucket, curated_bucket, fetch))
        except _RECOVERABLE:
            logger.exception("Falha ao processar o ponto %s", point.point_id)
            failed.append(point.point_id)

    result = RunResult(issued_at, _sum_reports(reports), tuple(failed))
    publish_metrics(cloudwatch, result)
    logger.info("Execução %s: %s, falhas=%s", issued_at.isoformat(), result.report, failed)
    if failed:
        raise IngestionError(f"Pontos com falha: {', '.join(failed)}")
    return result


def handler(event: Mapping[str, Any], _context: object) -> dict[str, Any]:
    result = run(
        parse_issued_at(event),
        s3=boto3.client("s3"),
        cloudwatch=boto3.client("cloudwatch"),
        raw_bucket=os.environ["RAW_BUCKET"],
        curated_bucket=os.environ["CURATED_BUCKET"],
        fetch=get_bytes,
    )
    return {"issued_at": result.issued_at.isoformat(), "rows": result.report.rows}
