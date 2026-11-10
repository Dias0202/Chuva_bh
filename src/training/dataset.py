"""Leitura do data lake, split temporal e formato de entrada do XGBoost."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

from features.build import HORIZONS, LABEL

HISTORICAL_SOURCE = "open_meteo_historical_forecast"
BASELINE_COLUMNS = ("point_id", "issued_at", "horizon_h", "precip_mm", "fc_precipitation_mm")


@dataclass(frozen=True)
class Split:
    train: pd.DataFrame
    validation: pd.DataFrame
    test: pd.DataFrame


def read_partitioned(path: Path) -> pd.DataFrame:
    """Lê um dataset Parquet particionado no formato Hive (year/month/point_id)."""
    dataset = ds.dataset(  # type: ignore[no-untyped-call]
        str(path), format="parquet", partitioning="hive"
    )
    table = dataset.to_table()
    frame = table.to_pandas()
    frame = frame.drop(columns=[c for c in ("year", "month") if c in frame.columns])
    frame["point_id"] = frame["point_id"].astype(str)
    return frame


def historical_forecasts(forecasts: pd.DataFrame) -> pd.DataFrame:
    """Mantém só a série histórica, uma linha por ponto e horário."""
    history = forecasts[forecasts["source"] == HISTORICAL_SOURCE]
    return history.drop_duplicates(["point_id", "valid_at"], keep="last")


def temporal_split(rows: pd.DataFrame, test_year: int) -> Split:
    """Treino até test_year-2, validação em test_year-1 e teste em test_year.

    As últimas horas antes de cada corte são descartadas, porque o rótulo delas
    está no período seguinte.
    """
    gap = pd.Timedelta(hours=max(HORIZONS))
    issued = rows["issued_at"]
    validation_start = pd.Timestamp(year=test_year - 1, month=1, day=1)
    test_start = pd.Timestamp(year=test_year, month=1, day=1)
    test_end = pd.Timestamp(year=test_year + 1, month=1, day=1)
    split = Split(
        train=rows[issued < validation_start - gap],
        validation=rows[(issued >= validation_start) & (issued < test_start - gap)],
        test=rows[(issued >= test_start) & (issued < test_end - gap)],
    )
    for name in ("train", "validation", "test"):
        if getattr(split, name).empty:
            raise ValueError(f"split {name} vazio para test_year={test_year}")
    return split


def xgboost_frame(rows: pd.DataFrame, features: tuple[str, ...]) -> pd.DataFrame:
    """Rótulo na primeira coluna, como o XGBoost built-in do SageMaker exige."""
    frame = rows[[LABEL, *features]].astype(np.float32)
    return frame.reset_index(drop=True)
