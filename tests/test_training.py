"""Testes de métricas, baselines, split, processing e avaliação."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import xgboost as xgb

from features.build import FEATURE_SETS, LABEL
from training import evaluate, preprocess
from training.baselines import ForecastBaseline, PersistenceBaseline
from training.dataset import historical_forecasts, temporal_split, xgboost_frame
from training.evaluation import compare
from training.metrics import (
    average_precision,
    best_threshold,
    brier_score,
    brier_skill,
    contingency,
    reliability,
)


def test_brier_score() -> None:
    assert brier_score([0, 1], [0.0, 1.0]) == 0.0
    assert brier_score([0, 1], [0.5, 0.5]) == 0.25


def test_average_precision_matches_the_reference_definition() -> None:
    # Mesmo exemplo da documentação do scikit-learn: 0.8333...
    assert average_precision([0, 0, 1, 1], [0.1, 0.4, 0.35, 0.8]) == pytest.approx(5 / 6)


def test_average_precision_handles_ties_and_no_positives() -> None:
    assert average_precision([1, 0], [0.5, 0.5]) == pytest.approx(0.5)
    assert np.isnan(average_precision([0, 0], [0.2, 0.3]))


@pytest.mark.parametrize(
    ("y", "p"), [([0, 1], [0.2]), ([], []), ([0], [1.5]), ([0], [float("nan")])]
)
def test_metrics_reject_invalid_input(y: list[float], p: list[float]) -> None:
    with pytest.raises(ValueError, match=r"vetores|amostras|probabilidades"):
        brier_score(y, p)


def test_contingency_scores() -> None:
    scores = contingency([1, 1, 0, 0], [0.9, 0.1, 0.8, 0.1], 0.5)

    assert scores == {"pod": 0.5, "far": 0.5, "csi": pytest.approx(1 / 3)}


def test_best_threshold_and_reliability() -> None:
    y = [0, 0, 0, 1, 1]
    p = [0.1, 0.2, 0.3, 0.7, 0.8]

    assert 0.3 < best_threshold(y, p) <= 0.7
    assert sum(b["count"] for b in reliability(y, p)) == 5
    assert brier_skill(0.02, 0.025) == pytest.approx(0.2)
    assert np.isnan(brier_skill(0.1, 0.0))


def _rows(n: int = 400, seed: int = 0, start: str = "2021-01-01") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rain_now = rng.random(n) < 0.2
    label = np.where(rain_now, rng.random(n) < 0.6, rng.random(n) < 0.05).astype(float)
    frame = pd.DataFrame(
        {
            "point_id": "A521",
            "issued_at": pd.date_range(start, periods=n, freq="h"),
            "horizon_h": rng.choice([1, 3, 6], n),
            "precip_mm": np.where(rain_now, 1.0, 0.0),
            "fc_precipitation_mm": np.where(label == 1, 2.0, 0.0),
            LABEL: label,
        }
    )
    for column in FEATURE_SETS["full"]:
        if column not in frame:
            frame[column] = rng.random(n)
    return frame


def test_persistence_baseline_learns_conditional_rates() -> None:
    train = _rows()
    model = PersistenceBaseline().fit(train)

    p = model.predict(train)

    assert (
        p[train["precip_mm"].to_numpy() >= 0.5].mean()
        > p[train["precip_mm"].to_numpy() == 0].mean()
    )
    assert set(model.to_dict()) == {"rates", "fallback"}


def test_forecast_baseline_uses_fallback_for_missing_forecast() -> None:
    train = _rows()
    model = ForecastBaseline().fit(train)
    unseen = train.head(2).assign(fc_precipitation_mm=np.nan)

    assert np.allclose(model.predict(unseen), [model.fallback[int(h)] for h in unseen["horizon_h"]])


def test_baselines_require_fit_and_data() -> None:
    with pytest.raises(RuntimeError):
        PersistenceBaseline().predict(_rows())
    with pytest.raises(ValueError, match="vazio"):
        PersistenceBaseline().fit(_rows().head(0))


def test_temporal_split_respects_years_and_purges_the_boundary() -> None:
    rows = _rows(3 * 8760, start="2021-01-01")
    split = temporal_split(rows, test_year=2023)

    assert split.train["issued_at"].max() < pd.Timestamp("2022-01-01")
    assert split.validation["issued_at"].dt.year.eq(2022).all()
    assert split.validation["issued_at"].max() <= pd.Timestamp("2022-12-31 18:00")
    assert split.test["issued_at"].dt.year.eq(2023).all()


def test_temporal_split_rejects_empty_partitions() -> None:
    with pytest.raises(ValueError, match="vazio"):
        temporal_split(_rows(100, start="2021-01-01"), test_year=2030)


def test_xgboost_frame_puts_the_label_first() -> None:
    frame = xgboost_frame(_rows(), FEATURE_SETS["obs_only"])

    assert frame.columns[0] == LABEL
    assert frame.dtypes.eq(np.float32).all()


def test_historical_forecasts_keep_one_row_per_hour() -> None:
    frame = pd.DataFrame(
        {
            "point_id": ["A521"] * 3,
            "valid_at": pd.to_datetime(["2025-01-01 00:00"] * 2 + ["2025-01-01 01:00"]),
            "source": ["open_meteo_historical_forecast"] * 2 + ["open_meteo_forecast"],
        }
    )

    assert len(historical_forecasts(frame)) == 1


def test_compare_reports_the_best_baseline() -> None:
    train, validation, test = _rows(seed=1), _rows(seed=2), _rows(seed=3)
    perfect = test[LABEL].to_numpy() * 0.98 + 0.01

    report = compare(train, validation, test, validation[LABEL].to_numpy(), perfect)

    assert report["model_beats_baselines"] is True
    assert report["best_baseline"]["name"] in ("persistence", "forecast")
    assert set(report["metrics"]["model"]["by_horizon"]) == {"1", "3", "6"}
    json.dumps(report)


def _write_hive(frame: pd.DataFrame, root: Path, time_column: str) -> None:
    data = frame.assign(year=frame[time_column].dt.year, month=frame[time_column].dt.month)
    pq.write_to_dataset(  # type: ignore[no-untyped-call]
        pa.Table.from_pandas(data, preserve_index=False),
        str(root),
        partition_cols=["year", "month", "point_id"],
    )


def _lake(tmp_path: Path) -> Path:
    hours = pd.date_range("2021-03-24", "2023-12-31 23:00", freq="h")
    rng = np.random.default_rng(0)
    rain = np.where(rng.random(len(hours)) < 0.05, 2.0, 0.0)
    observations = pd.DataFrame(
        {
            "observed_at": hours,
            "source": "inmet_bulk",
            "precipitation_mm": rain,
            "pressure_hpa": 920.0,
            "radiation_kj_m2": 0.0,
            "temperature_c": 22.0,
            "dew_point_c": 16.0,
            "relative_humidity_pct": 70.0,
            "wind_direction_deg": 90.0,
            "wind_gust_ms": 3.0,
            "wind_speed_ms": 1.0,
            "point_id": "A521",
        }
    )
    forecasts = pd.DataFrame(
        {
            "valid_at": hours,
            "issued_at": pd.NaT,
            "source": "open_meteo_historical_forecast",
            "precipitation_mm": np.roll(rain, -1),
            "cape_j_kg": 100.0,
            "cloud_cover_pct": 50.0,
            "relative_humidity_pct": 75.0,
            "point_id": "A521",
        }
    )
    root = tmp_path / "input"
    _write_hive(observations, root / "observations", "observed_at")
    _write_hive(forecasts, root / "forecasts", "valid_at")
    return root


def test_preprocess_and_evaluate_run_end_to_end(tmp_path: Path) -> None:
    base = tmp_path / "processing"
    assert (
        preprocess.main(
            ["--input-dir", str(_lake(tmp_path)), "--output-dir", str(base), "--test-year", "2023"]
        )
        == 0
    )

    train = pd.read_parquet(base / "train" / "train.parquet")
    schema = json.loads((base / "meta" / "schema.json").read_text(encoding="utf-8"))
    assert train.columns[0] == LABEL
    assert list(train.columns[1:]) == schema["features"]

    validation = pd.read_parquet(base / "validation" / "validation.parquet")
    booster = xgb.train(
        {"objective": "binary:logistic", "max_depth": 2},
        xgb.DMatrix(train.iloc[:, 1:], label=train[LABEL]),
        num_boost_round=5,
    )
    raw = booster.save_raw("json")
    model_dir = base / "model"
    model_dir.mkdir()
    with tarfile.open(model_dir / "model.tar.gz", "w:gz") as bundle:
        info = tarfile.TarInfo(evaluate.MODEL_FILE)
        info.size = len(raw)
        bundle.addfile(info, io.BytesIO(bytes(raw)))
    assert len(validation) > 0

    assert evaluate.main(["--base-dir", str(base)]) == 0
    report = json.loads((base / "evaluation" / "evaluation.json").read_text(encoding="utf-8"))
    assert "brier" in report["metrics"]["model"]
    assert report["best_baseline"]["name"] in ("persistence", "forecast")


def test_load_booster_rejects_a_package_without_the_model(tmp_path: Path) -> None:
    with tarfile.open(tmp_path / "model.tar.gz", "w:gz") as bundle:
        info = tarfile.TarInfo("outro-arquivo")
        info.size = 1
        bundle.addfile(info, io.BytesIO(b"x"))

    with pytest.raises(KeyError):
        evaluate.load_booster(tmp_path)
