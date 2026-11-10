#!/usr/bin/env python
"""Carrega o histórico de observações e previsões no bucket curated.

Uso:
    backfill.py --dry-run                  processa sem gravar na AWS
    backfill.py                            grava observações e previsões
    backfill.py --dataset observations --start-year 2026

Os objetos têm chave determinística, então repetir a carga apenas os substitui.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final

import boto3
from botocore.exceptions import BotoCoreError, ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ingestion.columns import FORECASTS, OBSERVATIONS, Dataset, Row
from ingestion.http import Fetch, FetchError, get_bytes
from ingestion.inmet import BULK_URL_TEMPLATE, InmetFormatError, iter_stations
from ingestion.open_meteo import OpenMeteoFormatError, fetch_historical
from ingestion.points import POINT_IDS, POINTS
from ingestion.quality import QualityReport, prepare
from ingestion.storage import write_rows

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

logger = logging.getLogger("backfill")

REGION: Final = "us-east-2"
PROJECT: Final = "chuva-bh"
OBSERVATIONS_START_YEAR: Final = 2019
# Antes dessa data a Open-Meteo não publica CAPE no histórico de previsões.
FORECASTS_START: Final = date(2021, 3, 24)
BULK_TIMEOUT_SECONDS: Final = 240
HISTORY_FILE_NAME: Final = "history.parquet"
REPORT_PATH: Final = Path(__file__).resolve().parents[1] / "docs" / "data-quality.md"

Line = tuple[str, str, int, QualityReport]


def load_rows(
    rows: list[Row],
    dataset: Dataset,
    point_id: str,
    year: int,
    s3: S3Client | None,
    bucket: str,
) -> Line:
    clean, report = prepare(rows, dataset)
    if s3 is not None and clean:
        write_rows(s3, bucket, clean, dataset, point_id, HISTORY_FILE_NAME)
    return dataset.name, point_id, year, report


def backfill_observations(
    fetch: Fetch, s3: S3Client | None, bucket: str, start_year: int, end_year: int
) -> list[Line]:
    lines: list[Line] = []
    for year in range(start_year, end_year + 1):
        logger.info("Baixando observações de %d", year)
        archive = fetch(BULK_URL_TEMPLATE.format(year=year))
        for point_id, rows in iter_stations(archive, POINT_IDS):
            lines.append(load_rows(rows, OBSERVATIONS, point_id, year, s3, bucket))
    return lines


def backfill_forecasts(
    fetch: Fetch, s3: S3Client | None, bucket: str, start_year: int, end_year: int, today: date
) -> list[Line]:
    last_day = today - timedelta(days=1)
    lines: list[Line] = []
    for year in range(max(start_year, FORECASTS_START.year), end_year + 1):
        start = max(date(year, 1, 1), FORECASTS_START)
        end = min(date(year, 12, 31), last_day)
        if start > end:
            continue
        for point in POINTS:
            logger.info("Baixando previsões de %d para %s", year, point.point_id)
            rows = fetch_historical(point, fetch, start, end)
            lines.append(load_rows(rows, FORECASTS, point.point_id, year, s3, bucket))
    return lines


def render_report(lines: Sequence[Line], generated_on: date) -> str:
    header = (
        "# Relatório de qualidade dos dados\n\n"
        f"Gerado por `scripts/backfill.py` em {generated_on.isoformat()}.\n\n"
        "| Dataset | Ponto | Ano | Linhas | Nulos | Fora da faixa | Duplicadas | Lacunas (h) |\n"
        "|---|---|---:|---:|---:|---:|---:|---:|\n"
    )
    body = "".join(
        f"| {name} | {point} | {year} | {r.rows} | {r.null_values} | {r.out_of_range} "
        f"| {r.duplicate_keys} | {r.temporal_gaps} |\n"
        for name, point, year, r in sorted(lines, key=lambda line: line[:3])
    )
    return header + body


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    today = datetime.now(UTC).date()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=("observations", "forecasts", "all"), default="all")
    parser.add_argument("--start-year", type=int, default=OBSERVATIONS_START_YEAR)
    parser.add_argument("--end-year", type=int, default=today.year)
    parser.add_argument("--dry-run", action="store_true", help="processa sem gravar na AWS")
    parser.add_argument("--profile", help="perfil AWS")
    args = parser.parse_args(argv)

    if args.start_year > args.end_year:
        parser.error("--start-year não pode ser maior que --end-year")

    fetch: Fetch = partial(get_bytes, timeout=BULK_TIMEOUT_SECONDS)
    try:
        s3: S3Client | None = None
        bucket = ""
        if not args.dry_run:
            session = boto3.Session(profile_name=args.profile, region_name=REGION)
            s3 = session.client("s3")
            account = session.client("sts").get_caller_identity()["Account"]
            bucket = f"{PROJECT}-curated-{account}-{REGION}"

        lines: list[Line] = []
        if args.dataset in ("observations", "all"):
            lines += backfill_observations(fetch, s3, bucket, args.start_year, args.end_year)
        if args.dataset in ("forecasts", "all"):
            lines += backfill_forecasts(fetch, s3, bucket, args.start_year, args.end_year, today)
    except (
        FetchError,
        InmetFormatError,
        OpenMeteoFormatError,
        ClientError,
        BotoCoreError,
    ) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1

    report = render_report(lines, today)
    if args.dry_run:
        print(report)
    else:
        REPORT_PATH.write_text(report, encoding="utf-8")
        print(f"Relatório gravado em {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
