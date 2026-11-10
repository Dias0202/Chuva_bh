"""Processing job: avalia o modelo treinado contra as baselines e grava evaluation.json."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tarfile
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pandas as pd

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from training.evaluation import compare

logger = logging.getLogger("evaluate")

BASE_DIR = Path("/opt/ml/processing")
MODEL_FILE = "xgboost-model"


def load_booster(model_dir: Path) -> object:
    import xgboost as xgb

    archive = model_dir / "model.tar.gz"
    if archive.exists():
        # Lê só o arquivo esperado, sem extrair caminhos arbitrários do pacote.
        with tarfile.open(archive) as bundle:
            source = bundle.extractfile(bundle.getmember(MODEL_FILE))
            if source is None:
                raise ValueError(f"{MODEL_FILE} não é um arquivo no pacote do modelo")
            (model_dir / MODEL_FILE).write_bytes(source.read())
    booster = xgb.Booster()
    booster.load_model(str(model_dir / MODEL_FILE))
    return booster


def predict(booster: object, frame: pd.DataFrame) -> np.ndarray:
    import xgboost as xgb

    # Com nomes de coluna: serve tanto para modelos com nomes de feature quanto sem.
    features = frame.iloc[:, 1:].astype(np.float32)
    matrix = xgb.DMatrix(features, missing=np.nan)
    return np.asarray(booster.predict(matrix), dtype=float)  # type: ignore[attr-defined]


def run(base_dir: Path) -> dict[str, object]:
    booster = load_booster(base_dir / "model")
    meta = {
        n: pd.read_parquet(base_dir / "meta" / f"{n}.parquet")
        for n in ("train", "validation", "test")
    }
    validation = pd.read_parquet(base_dir / "validation" / "validation.parquet")
    test = pd.read_parquet(base_dir / "test" / "test.parquet")
    return compare(
        meta["train"],
        meta["validation"],
        meta["test"],
        predict(booster, validation),
        predict(booster, test),
    )


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", type=Path, default=BASE_DIR)
    args = parser.parse_args(argv)

    report = run(args.base_dir)
    output = args.base_dir / "evaluation"
    output.mkdir(parents=True, exist_ok=True)
    (output / "evaluation.json").write_text(json.dumps(report), encoding="utf-8")
    logger.info("Modelo vence as baselines: %s", report["model_beats_baselines"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
