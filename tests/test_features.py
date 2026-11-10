"""Testes das features: alinhamento temporal, ausência de vazamento e compatibilidade."""

from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from features.build import (
    FEATURE_SETS,
    LABEL,
    build_dataset,
    forecast_features,
    observation_features,
    rain_labels,
)

ROOT = Path(__file__).resolve().parents[1]
START = pd.Timestamp("2025-01-01 00:00")


def _observations(hours: int = 30, point_id: str = "A521") -> pd.DataFrame:
    times = pd.date_range(START, periods=hours, freq="h")
    rain = np.zeros(hours)
    rain[10] = 2.0
    return pd.DataFrame(
        {
            "point_id": point_id,
            "observed_at": times,
            "precipitation_mm": rain,
            "temperature_c": np.linspace(20, 25, hours),
            "dew_point_c": np.full(hours, 15.0),
            "relative_humidity_pct": np.full(hours, 70.0),
            "pressure_hpa": np.linspace(920, 917, hours),
            "wind_speed_ms": np.ones(hours),
            "wind_gust_ms": np.full(hours, 3.0),
            "wind_direction_deg": np.full(hours, 90.0),
            "radiation_kj_m2": np.zeros(hours),
        }
    )


def _forecasts(hours: int = 40) -> pd.DataFrame:
    times = pd.date_range(START, periods=hours, freq="h")
    return pd.DataFrame(
        {
            "point_id": "A521",
            "valid_at": times,
            "precipitation_mm": np.arange(hours, dtype=float),
            "cape_j_kg": np.full(hours, 100.0),
            "cloud_cover_pct": np.full(hours, 50.0),
            "relative_humidity_pct": np.full(hours, 80.0),
        }
    )


def test_observation_features_use_only_the_past() -> None:
    features = observation_features(_observations()).set_index("issued_at")

    assert features.loc[START + pd.Timedelta(hours=10), "precip_mm"] == 2.0
    assert features.loc[START + pd.Timedelta(hours=11), "precip_lag1_mm"] == 2.0
    assert features.loc[START + pd.Timedelta(hours=9), "precip_lag1_mm"] == 0.0
    assert np.isnan(features.loc[START + pd.Timedelta(hours=1), "precip_sum_3h_mm"])
    assert features.loc[START + pd.Timedelta(hours=12), "precip_sum_3h_mm"] == 2.0
    assert features.loc[START, "dew_point_depression_c"] == pytest.approx(5.0)
    assert features.loc[START, "wind_dir_sin"] == pytest.approx(1.0)


def test_changing_the_future_does_not_change_features() -> None:
    original = observation_features(_observations())
    future = _observations()
    future.loc[future["observed_at"] > START + pd.Timedelta(hours=15), "precipitation_mm"] = 50.0
    changed = observation_features(future)

    cutoff = original["issued_at"] <= START + pd.Timedelta(hours=15)
    pd.testing.assert_frame_equal(original[cutoff], changed[cutoff])


def test_missing_hours_become_gaps_instead_of_shifting_lags() -> None:
    observations = _observations().drop(index=9)

    features = observation_features(observations).set_index("issued_at")

    assert np.isnan(features.loc[START + pd.Timedelta(hours=10), "precip_lag1_mm"])


def test_rain_labels_look_ahead_by_the_horizon() -> None:
    labels = rain_labels(_observations(), (1, 3)).set_index(["horizon_h", "issued_at"])

    assert labels.loc[(1, START + pd.Timedelta(hours=9)), LABEL] == 1.0
    assert labels.loc[(3, START + pd.Timedelta(hours=7)), LABEL] == 1.0
    assert labels.loc[(1, START + pd.Timedelta(hours=10)), LABEL] == 0.0
    assert np.isnan(labels.loc[(1, START + pd.Timedelta(hours=29)), LABEL])


def test_forecast_features_align_valid_time_and_accumulate() -> None:
    features = forecast_features(_forecasts(), (1, 3)).set_index(["horizon_h", "issued_at"])

    assert features.loc[(1, START), "fc_precipitation_mm"] == 1.0
    assert features.loc[(3, START), "fc_precipitation_mm"] == 3.0
    assert features.loc[(3, START), "fc_precipitation_sum_mm"] == 1.0 + 2.0 + 3.0


def test_build_dataset_has_every_feature_and_no_missing_labels() -> None:
    rows = build_dataset(_observations(), _forecasts())

    assert set(FEATURE_SETS["full"]) <= set(rows.columns)
    assert rows[LABEL].notna().all()
    assert set(rows["horizon_h"]) == {1, 3, 6}
    assert not rows.duplicated(["point_id", "issued_at", "horizon_h"]).any()


def test_build_dataset_without_forecasts_fills_nan() -> None:
    rows = build_dataset(_observations(), None, with_labels=False)

    assert rows["fc_precipitation_mm"].isna().all()
    assert LABEL not in rows.columns


@pytest.mark.parametrize("package", ["features", "training"])
def test_container_code_parses_as_python_39(package: str) -> None:
    for path in (ROOT / "src" / package).glob("*.py"):
        if path.name == "pipeline.py":
            continue
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 9))
