"""Validação de faixas físicas, duplicidade e lacunas temporais."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from ingestion.columns import DOUBLE, Dataset, Row

_HOUR: Final = timedelta(hours=1)


@dataclass(frozen=True)
class QualityReport:
    rows: int = 0
    null_values: int = 0
    out_of_range: int = 0
    duplicate_keys: int = 0
    temporal_gaps: int = 0


def _count_gaps(times_by_point: dict[object, set[datetime]]) -> int:
    gaps = 0
    for times in times_by_point.values():
        expected = int((max(times) - min(times)) / _HOUR) + 1
        gaps += expected - len(times)
    return gaps


def prepare(rows: Iterable[Row], dataset: Dataset) -> tuple[list[Row], QualityReport]:
    """Devolve as linhas limpas e o relatório.

    Valores fora da faixa física viram nulos, para não descartar as demais
    variáveis da hora. Em chaves repetidas, prevalece a última ocorrência.
    """
    numeric = [column for column in dataset.columns if column.glue_type == DOUBLE]
    unique: dict[tuple[object, ...], Row] = {}
    null_values = out_of_range = duplicates = 0

    for source_row in rows:
        row = dict(source_row)
        if not isinstance(row.get(dataset.time_column), datetime):
            raise ValueError(f"linha sem {dataset.time_column}")
        for column in numeric:
            value = row.get(column.name)
            if value is None:
                null_values += 1
            elif (column.min_value is not None and float(value) < column.min_value) or (  # type: ignore[arg-type]
                column.max_value is not None and float(value) > column.max_value  # type: ignore[arg-type]
            ):
                row[column.name] = None
                out_of_range += 1
        key = tuple(row.get(name) for name in dataset.key_columns)
        if key in unique:
            duplicates += 1
        unique[key] = row

    clean = sorted(unique.values(), key=lambda r: r[dataset.time_column])  # type: ignore[arg-type, return-value]

    times_by_point: dict[object, set[datetime]] = defaultdict(set)
    for row in clean:
        moment = row[dataset.time_column]
        assert isinstance(moment, datetime)  # noqa: S101
        times_by_point[row.get("point_id")].add(moment)

    return clean, QualityReport(
        rows=len(clean),
        null_values=null_values,
        out_of_range=out_of_range,
        duplicate_keys=duplicates,
        temporal_gaps=_count_gaps(times_by_point),
    )
