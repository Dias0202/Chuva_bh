"""Leitura das partições do data lake (year/month/point_id) direto do S3."""

from __future__ import annotations

import io
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import TYPE_CHECKING

import pandas as pd
import pyarrow.parquet as pq

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client


def partition_prefix(dataset: str, moment: datetime, point_id: str) -> str:
    return f"{dataset}/year={moment.year:04d}/month={moment.month:02d}/point_id={point_id}/"


def months_between(start: datetime, end: datetime) -> list[datetime]:
    """Primeiro dia de cada mês do intervalo, inclusive."""
    if start > end:
        raise ValueError("início depois do fim")
    current = datetime(start.year, start.month, 1)
    months = []
    while current <= end:
        months.append(current)
        current = datetime(current.year + current.month // 12, current.month % 12 + 1, 1)
    return months


def list_keys(s3: S3Client, bucket: str, prefix: str) -> list[str]:
    paginator = s3.get_paginator("list_objects_v2")
    return [
        item["Key"]
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
        for item in page.get("Contents", [])
    ]


def read_objects(s3: S3Client, bucket: str, keys: Iterable[str]) -> pd.DataFrame:
    frames = []
    for key in keys:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        frames.append(pq.read_table(io.BytesIO(body)).to_pandas())  # type: ignore[no-untyped-call]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def read_range(
    s3: S3Client,
    bucket: str,
    dataset: str,
    point_id: str,
    start: datetime,
    end: datetime,
    keep: Callable[[str], bool] = lambda _key: True,
) -> pd.DataFrame:
    """Lê os meses do intervalo para um ponto. A coluna point_id vem do caminho."""
    keys = [
        key
        for month in months_between(start, end)
        for key in list_keys(s3, bucket, partition_prefix(dataset, month, point_id))
        if keep(key)
    ]
    frame = read_objects(s3, bucket, keys)
    if not frame.empty:
        frame["point_id"] = point_id
    return frame
