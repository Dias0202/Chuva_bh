#!/usr/bin/env python
"""Carrega o histórico de observações e previsões no bucket curated.

Uso:
    backfill.py --dry-run                  processa sem gravar na AWS
    backfill.py                            grava observações e previsões
    backfill.py --dataset observations --start-year 2026
    backfill.py --dataset metar --start-year 2021   METAR da Pampulha, sem credencial

Os objetos têm chave determinística, então repetir a carga apenas os substitui.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final

import boto3
from botocore.exceptions import BotoCoreError, ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ingestion.columns import FORECASTS, METAR, OBSERVATIONS, Dataset, Row
from ingestion.http import Fetch, FetchError, get_bytes
from ingestion.inmet import BULK_URL_TEMPLATE, InmetFormatError, iter_stations
from ingestion.metar import POINT_ID as METAR_POINT
from ingestion.metar import MetarFormatError, archive_url, parse_archive
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
# O IEM limita a taxa de requisições: pausa entre anos e espera maior ao bater no limite.
METAR_PAUSE_SECONDS: Final = 15
METAR_RATE_LIMIT_WAIT_SECONDS: Final = 90
METAR_ATTEMPTS: Final = 4

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


def backfill_metar(
    fetch: Fetch,
    s3: S3Client | None,
    bucket: str,
    start_year: int,
    end_year: int,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Line]:
    lines: list[Line] = []
    for year in range(start_year, end_year + 1):
        if lines:
            sleep(METAR_PAUSE_SECONDS)
        logger.info("Baixando METAR de %d", year)
        for attempt in range(1, METAR_ATTEMPTS + 1):
            try:
                rows = parse_archive(fetch(archive_url(year)).decode("utf-8"))
                break
            except MetarFormatError as error:
                if "limite" not in str(error) or attempt == METAR_ATTEMPTS:
                    raise
                logger.warning(
                    "Limite do IEM; nova tentativa em %ds", METAR_RATE_LIMIT_WAIT_SECONDS
                )
                sleep(METAR_RATE_LIMIT_WAIT_SECONDS)
        lines.append(load_rows(rows, METAR, METAR_POINT, year, s3, bucket))
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
    parser.add_argument(
        "--dataset", choices=("observations", "forecasts", "metar", "all"), default="all"
    )
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
        if args.dataset in ("metar", "all"):
            lines += backfill_metar(fetch, s3, bucket, args.start_year, args.end_year)
    except (
        FetchError,
        InmetFormatError,
        OpenMeteoFormatError,
        MetarFormatError,
        ClientError,
        BotoCoreError,
    ) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1

    report = render_report(lines, today)
    if args.dry_run:
        print(report)
    else:
        existing = REPORT_PATH.read_text(encoding="utf-8") if REPORT_PATH.exists() else ""
        REPORT_PATH.write_text(merge_report(existing, report), encoding="utf-8")
        print(f"Relatório gravado em {REPORT_PATH}")
    return 0


def merge_report(existing: str, new: str) -> str:
    """Mantém as linhas de datasets que não foram recarregados nesta execução."""

    def rows(text: str) -> dict[tuple[str, str, str], str]:
        found = {}
        for line in text.splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) == 8 and cells[2].isdigit():
                found[(cells[0], cells[1], cells[2])] = line
        return found

    fresh = rows(new)
    reloaded = {key[0] for key in fresh}
    kept = {key: line for key, line in rows(existing).items() if key[0] not in reloaded}
    merged = {**kept, **fresh}
    header = new.split("|---", 1)[0] + "|---|---|---:|---:|---:|---:|---:|---:|\n"
    return header + "".join(f"{merged[key]}\n" for key in sorted(merged))


if __name__ == "__main__":
    sys.exit(main())
