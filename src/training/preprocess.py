"""Processing job: monta features, aplica o split temporal e grava os conjuntos."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

import pandas as pd

if __package__ in (None, ""):
    # No container do SageMaker o script roda solto, a partir de source_dir.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from features.build import FEATURE_SETS, LABEL, build_dataset
from training.config import DEFAULT_FEATURE_SET, DEFAULT_TEST_YEAR
from training.dataset import (
    BASELINE_COLUMNS,
    Split,
    historical_forecasts,
    read_partitioned,
    temporal_split,
    xgboost_frame,
)

logger = logging.getLogger("preprocess")

INPUT_DIR = Path("/opt/ml/processing/input")
OUTPUT_DIR = Path("/opt/ml/processing/output")
SPLITS = ("train", "validation", "test")


def prepare(
    observations: pd.DataFrame,
    forecasts: pd.DataFrame,
    test_year: int,
    metar: pd.DataFrame | None = None,
    feature_set: str = "full",
) -> tuple[pd.DataFrame, Split]:
    """Restringe à janela com previsão histórica, para comparar todos no mesmo período.

    No conjunto `metar`, restringe também aos pontos e ao período com METAR.
    """
    history = historical_forecasts(forecasts)
    rows = build_dataset(observations, history, metar=metar)
    rows = rows[rows["issued_at"] >= history["valid_at"].min()]
    if feature_set == "metar":
        if metar is None or metar.empty:
            raise ValueError("o conjunto metar exige o dataset de METAR")
        rows = rows[rows["point_id"].isin(set(metar["point_id"]))]
        rows = rows[rows["issued_at"] >= pd.Timestamp(metar["observed_at"].min())]
    return rows, temporal_split(rows, test_year)


def read_optional(path: Path) -> pd.DataFrame | None:
    return read_partitioned(path) if path.exists() and any(path.rglob("*.parquet")) else None


def write_outputs(split: Split, feature_set: str, output_dir: Path) -> dict[str, int]:
    features = FEATURE_SETS[feature_set]
    counts = {}
    for name in SPLITS:
        rows = getattr(split, name)
        (output_dir / name).mkdir(parents=True, exist_ok=True)
        (output_dir / "meta").mkdir(parents=True, exist_ok=True)
        xgboost_frame(rows, features).to_parquet(output_dir / name / f"{name}.parquet")
        meta = rows[[*BASELINE_COLUMNS, LABEL]].reset_index(drop=True)
        meta.to_parquet(output_dir / "meta" / f"{name}.parquet")
        counts[name] = len(rows)
    schema = {"label": LABEL, "features": list(features), "feature_set": feature_set}
    (output_dir / "meta" / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
    return counts


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-year", type=int, default=DEFAULT_TEST_YEAR)
    parser.add_argument("--feature-set", choices=sorted(FEATURE_SETS), default=DEFAULT_FEATURE_SET)
    parser.add_argument("--input-dir", type=Path, default=INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args(argv)

    observations = read_partitioned(args.input_dir / "observations")
    forecasts = read_partitioned(args.input_dir / "forecasts")
    metar = read_optional(args.input_dir / "metar")
    _, split = prepare(observations, forecasts, args.test_year, metar, args.feature_set)
    counts = write_outputs(split, args.feature_set, args.output_dir)
    logger.info("Linhas por conjunto: %s", counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
