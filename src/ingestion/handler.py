"""Lambda horária: grava a previsão operacional e, com chave do INMET, a observação."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from ingestion.columns import FORECASTS, METAR, OBSERVATIONS, Dataset, Row
from ingestion.http import Fetch, FetchError, get_bytes
from ingestion.inmet_api import InmetApiError, fetch_recent
from ingestion.metar import POINT_ID as METAR_POINT
from ingestion.metar import MetarFormatError, fetch_live
from ingestion.open_meteo import OpenMeteoFormatError, fetch_forecast
from ingestion.points import POINTS, Point
from ingestion.quality import QualityReport, prepare
from ingestion.storage import write_rows

if TYPE_CHECKING:
    from types_boto3_cloudwatch.client import CloudWatchClient
    from types_boto3_cloudwatch.type_defs import MetricDatumTypeDef
    from types_boto3_s3.client import S3Client
    from types_boto3_ssm.client import SSMClient

logger = logging.getLogger()
logger.setLevel(logging.INFO)

METRIC_NAMESPACE: Final = "ChuvaBh/Ingestion"
RAW_PREFIX: Final = "open_meteo/forecast"

_RECOVERABLE: Final = (FetchError, OpenMeteoFormatError, InmetApiError, ClientError, BotoCoreError)
_token_cache: dict[str, str | None] = {}


class IngestionError(RuntimeError):
    """Algum ponto falhou. A Lambda deve repetir a execução, que é idempotente."""


@dataclass(frozen=True)
class RunResult:
    issued_at: datetime
    report: QualityReport
    failed_points: tuple[str, ...]
    observation_report: QualityReport | None = None
    failed_observations: tuple[str, ...] = ()
    metar_rows: int | None = None
    metar_failed: bool = False


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


def _ingest_observations(
    point: Point,
    issued_at: datetime,
    s3: S3Client,
    curated_bucket: str,
    token: str,
    fetch: Fetch,
) -> QualityReport:
    """Regrava o dia anterior e o atual inteiros: um arquivo por dia, sem duplicar horas."""
    start = (issued_at - timedelta(days=1)).date()
    rows = fetch_recent(point.point_id, token, fetch, start, issued_at.date())
    clean, report = prepare(rows, OBSERVATIONS)
    by_day: dict[str, list[Row]] = {}
    for row in clean:
        moment = row["observed_at"]
        assert isinstance(moment, datetime)  # noqa: S101
        by_day.setdefault(f"{moment:%Y%m%d}", []).append(row)
    for day, day_rows in by_day.items():
        write_rows(s3, curated_bucket, day_rows, OBSERVATIONS, point.point_id, f"api-{day}.parquet")
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


def _metric_data(dataset: Dataset, report: QualityReport, failed: int) -> list[MetricDatumTypeDef]:
    values = {
        "RowsWritten": report.rows,
        "NullValues": report.null_values,
        "OutOfRange": report.out_of_range,
        "DuplicateKeys": report.duplicate_keys,
        "TemporalGaps": report.temporal_gaps,
        "PointsFailed": failed,
    }
    return [
        {
            "MetricName": name,
            "Dimensions": [{"Name": "Dataset", "Value": dataset.name}],
            "Value": float(value),
            "Unit": "Count",
        }
        for name, value in values.items()
    ]


def publish_metrics(cloudwatch: CloudWatchClient, result: RunResult) -> None:
    metric_data = _metric_data(FORECASTS, result.report, len(result.failed_points))
    if result.observation_report is not None:
        metric_data += _metric_data(
            OBSERVATIONS, result.observation_report, len(result.failed_observations)
        )
    if result.metar_rows is not None:
        # Só duas métricas para o METAR, para conter o custo de métricas customizadas.
        metric_data += [
            {
                "MetricName": name,
                "Dimensions": [{"Name": "Dataset", "Value": METAR.name}],
                "Value": float(value),
                "Unit": "Count",
            }
            for name, value in (
                ("RowsWritten", result.metar_rows),
                ("PointsFailed", int(result.metar_failed)),
            )
        ]
    cloudwatch.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=metric_data)


def _ingest_metar(s3: S3Client, curated_bucket: str, fetch: Fetch) -> int:
    """Grava as últimas horas do METAR, um arquivo por hora observada: reescrever é idempotente."""
    clean, _ = prepare(fetch_live(fetch), METAR)
    by_hour: dict[str, list[Row]] = {}
    for row in clean:
        moment = row["observed_at"]
        assert isinstance(moment, datetime)  # noqa: S101
        by_hour.setdefault(f"{moment:%Y%m%dT%H}", []).append(row)
    for hour, rows in by_hour.items():
        write_rows(s3, curated_bucket, rows, METAR, METAR_POINT, f"live-{hour}.parquet")
    return len(clean)


def run(
    issued_at: datetime,
    *,
    s3: S3Client,
    cloudwatch: CloudWatchClient,
    raw_bucket: str,
    curated_bucket: str,
    fetch: Fetch,
    points: Iterable[Point] = POINTS,
    inmet_token: str | None = None,
    observation_fetch: Fetch | None = None,
    metar_fetch: Fetch | None = None,
) -> RunResult:
    """Processa todos os pontos, para que a falha de um não impeça os demais."""
    selected = list(points)
    reports: list[QualityReport] = []
    failed: list[str] = []
    for point in selected:
        try:
            reports.append(_ingest_point(point, issued_at, s3, raw_bucket, curated_bucket, fetch))
        except _RECOVERABLE:
            logger.exception("Falha ao processar o ponto %s", point.point_id)
            failed.append(point.point_id)

    observation_reports: list[QualityReport] = []
    failed_observations: list[str] = []
    if inmet_token:
        for point in selected:
            try:
                observation_reports.append(
                    _ingest_observations(
                        point,
                        issued_at,
                        s3,
                        curated_bucket,
                        inmet_token,
                        observation_fetch or fetch,
                    )
                )
            except _RECOVERABLE:
                # A mensagem de erro não traz a URL, que contém a chave do INMET.
                logger.exception("Falha ao obter a observação do ponto %s", point.point_id)
                failed_observations.append(point.point_id)

    metar_rows: int | None = None
    metar_failed = False
    if metar_fetch is not None:
        try:
            metar_rows = _ingest_metar(s3, curated_bucket, metar_fetch)
        except (*_RECOVERABLE, MetarFormatError):
            logger.exception("Falha ao obter o METAR")
            metar_rows, metar_failed = 0, True

    result = RunResult(
        issued_at,
        _sum_reports(reports),
        tuple(failed),
        _sum_reports(observation_reports) if inmet_token else None,
        tuple(failed_observations),
        metar_rows,
        metar_failed,
    )
    publish_metrics(cloudwatch, result)
    logger.info("Execução %s: %s, falhas=%s", issued_at.isoformat(), result.report, failed)
    if failed or failed_observations or metar_failed:
        problems = failed + failed_observations + (["metar"] if metar_failed else [])
        raise IngestionError(f"Pontos com falha: {', '.join(problems)}")
    return result


def load_token(ssm: SSMClient, name: str | None) -> str | None:
    """Lê a chave do INMET uma vez por container. Sem parâmetro, a coleta fica desligada."""
    if not name:
        return None
    if name not in _token_cache:
        try:
            parameter = ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]
            _token_cache[name] = parameter.get("Value") or None
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ParameterNotFound":
                raise
            _token_cache[name] = None
    return _token_cache[name]


def handler(event: Mapping[str, Any], _context: object) -> dict[str, Any]:
    result = run(
        parse_issued_at(event),
        s3=boto3.client("s3"),
        cloudwatch=boto3.client("cloudwatch"),
        raw_bucket=os.environ["RAW_BUCKET"],
        curated_bucket=os.environ["CURATED_BUCKET"],
        fetch=get_bytes,
        inmet_token=load_token(boto3.client("ssm"), os.environ.get("INMET_TOKEN_PARAMETER")),
        observation_fetch=lambda url: get_bytes(url, label="API do INMET"),
        metar_fetch=get_bytes,
    )
    return {"issued_at": result.issued_at.isoformat(), "rows": result.report.rows}
