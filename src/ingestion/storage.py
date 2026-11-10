"""Escrita idempotente em Parquet, particionada por ano, mês e ponto."""

from __future__ import annotations

import io
from collections import defaultdict
from datetime import datetime
from typing import TYPE_CHECKING, Final

import pyarrow as pa
import pyarrow.parquet as pq

from ingestion.columns import DOUBLE, STRING, TIMESTAMP, Dataset, Row

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

PARQUET_CONTENT_TYPE: Final = "application/vnd.apache.parquet"

_ARROW_TYPES: Final = {
    TIMESTAMP: pa.timestamp("ms"),
    DOUBLE: pa.float64(),
    STRING: pa.string(),
}


def arrow_schema(dataset: Dataset) -> pa.Schema:
    return pa.schema([(column.name, _ARROW_TYPES[column.glue_type]) for column in dataset.columns])


def parquet_bytes(rows: list[Row], dataset: Dataset) -> bytes:
    """Serializa as linhas sem as colunas de partição, que ficam no caminho do objeto."""
    table = pa.Table.from_pylist(rows, schema=arrow_schema(dataset))
    buffer = io.BytesIO()
    pq.write_table(table, buffer, compression="snappy")  # type: ignore[no-untyped-call]
    return buffer.getvalue()


def object_key(dataset: Dataset, point_id: str, moment: datetime, file_name: str) -> str:
    return (
        f"{dataset.name}/year={moment.year:04d}/month={moment.month:02d}"
        f"/point_id={point_id}/{file_name}"
    )


def write_rows(
    s3: S3Client,
    bucket: str,
    rows: list[Row],
    dataset: Dataset,
    point_id: str,
    file_name: str,
) -> list[str]:
    """Grava um objeto por mês. A chave determinística torna a reexecução idempotente."""
    by_month: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        moment = row.get(dataset.time_column)
        if not isinstance(moment, datetime):
            raise ValueError(f"linha sem {dataset.time_column}")
        by_month[object_key(dataset, point_id, moment, file_name)].append(row)

    for key, month_rows in by_month.items():
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=parquet_bytes(month_rows, dataset),
            ContentType=PARQUET_CONTENT_TYPE,
        )
    return sorted(by_month)
