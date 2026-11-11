#!/usr/bin/env python
"""Experimento local: treina o XGBoost com os dados do data lake e compara com as baselines.

Uso:
    train_local.py                 sincroniza o bucket curated e roda os experimentos
    train_local.py --skip-sync     usa o cache local em .cache/curated

Lê do S3, não grava na AWS. O relatório vai para docs/evaluation.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import boto3
import numpy as np
import pandas as pd
import xgboost as xgb
from botocore.exceptions import BotoCoreError, ClientError

ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from features.build import FEATURE_SETS, LABEL  # noqa: E402
from training.config import DEFAULT_TEST_YEAR, HYPERPARAMETERS  # noqa: E402
from training.dataset import read_partitioned  # noqa: E402
from training.evaluation import compare  # noqa: E402
from training.preprocess import prepare  # noqa: E402

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

logger = logging.getLogger("train_local")

REGION: Final = "us-east-2"
CACHE_DIR: Final = ROOT / ".cache" / "curated"
REPORT_PATH: Final = ROOT / "docs" / "evaluation.md"
RESULTS_PATH: Final = ROOT / ".cache" / "evaluation.json"
DATASETS: Final = ("observations", "forecasts", "metar")


def sync(s3: S3Client, bucket: str, destination: Path) -> int:
    """Baixa só objetos novos ou com tamanho diferente do cache."""
    downloaded = 0
    paginator = s3.get_paginator("list_objects_v2")
    for prefix in DATASETS:
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
            for item in page.get("Contents", []):
                target = destination / item["Key"]
                if target.exists() and target.stat().st_size == item["Size"]:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                s3.download_file(bucket, item["Key"], str(target))
                downloaded += 1
    return downloaded


def train_booster(
    train: pd.DataFrame,
    validation: pd.DataFrame,
    features: tuple[str, ...],
    scale_pos_weight: float,
) -> xgb.Booster:
    params = {
        key: value
        for key, value in HYPERPARAMETERS.items()
        if key not in ("num_round", "early_stopping_rounds")
    }
    params["scale_pos_weight"] = scale_pos_weight
    params["seed"] = 42
    dtrain = xgb.DMatrix(train[list(features)], label=train[LABEL], missing=np.nan)
    dval = xgb.DMatrix(validation[list(features)], label=validation[LABEL], missing=np.nan)
    return xgb.train(
        params,
        dtrain,
        num_boost_round=int(HYPERPARAMETERS["num_round"]),
        evals=[(dval, "validation")],
        early_stopping_rounds=int(HYPERPARAMETERS["early_stopping_rounds"]),
        verbose_eval=False,
    )


def _predict(booster: xgb.Booster, frame: pd.DataFrame, features: tuple[str, ...]) -> np.ndarray:
    matrix = xgb.DMatrix(frame[list(features)], missing=np.nan)
    return np.asarray(booster.predict(matrix, iteration_range=(0, booster.best_iteration + 1)))


def run_experiments(
    rows_train: pd.DataFrame,
    rows_val: pd.DataFrame,
    rows_test: pd.DataFrame,
    feature_sets: tuple[str, ...] = ("full", "obs_only"),
    compare_weights: bool = True,
) -> list[dict[str, Any]]:
    positives = float(rows_train[LABEL].sum())
    imbalance = (len(rows_train) - positives) / positives
    weights = [("1", 1.0), ("auto", imbalance)] if compare_weights else [("1", 1.0)]
    results = []
    for feature_set in feature_sets:
        features = FEATURE_SETS[feature_set]
        for weight_name, weight in weights:
            logger.info("Treinando feature_set=%s scale_pos_weight=%s", feature_set, weight_name)
            booster = train_booster(rows_train, rows_val, features, weight)
            report = compare(
                rows_train,
                rows_val,
                rows_test,
                _predict(booster, rows_val, features),
                _predict(booster, rows_test, features),
            )
            importance = booster.get_score(importance_type="gain")
            results.append(
                {
                    "feature_set": feature_set,
                    "scale_pos_weight": weight_name,
                    "best_iteration": booster.best_iteration,
                    "report": report,
                    "top_features": sorted(
                        importance.items(),
                        key=lambda kv: -float(kv[1]),  # type: ignore[arg-type]
                    )[:8],
                }
            )
    return results


def render_metar(results: list[dict[str, Any]], counts: dict[str, int]) -> str:
    metrics = results[0]["report"]["metrics"]
    lines = [
        "",
        "## Modo METAR (Pampulha)",
        "",
        "Mesmas regras de split, só com o ponto A521 e o período com METAR do aeroporto. Linhas: "
        f"treino {counts['train']}, validação {counts['validation']}, teste {counts['test']}.",
        "",
        "| Preditor | Brier | Skill vs melhor baseline | PR-AUC | CSI |",
        "|---|---:|---:|---:|---:|",
    ]
    for name in ("persistence", "forecast"):
        m = metrics[name]
        lines.append(
            f"| baseline: {name} | {_fmt(m['brier'])} | — "
            f"| {_fmt(m['pr_auc'])} | {_fmt(m['csi'])} |"
        )
    for result in results:
        report = result["report"]
        m = report["metrics"]["model"]
        lines.append(
            f"| XGBoost {result['feature_set']} | {_fmt(m['brier'])} "
            f"| {_fmt(report['brier_skill_vs_best_baseline'])} | {_fmt(m['pr_auc'])} "
            f"| {_fmt(m['csi'])} |"
        )
    lines += [
        "",
        "O conjunto `metar` não usa a observação do INMET nas features, só o METAR do aeroporto e",
        "a previsão numérica. A persistência usa a chuva medida pelo INMET, que o modo METAR não",
        "tem em tempo real; ela fica como referência de qualidade, não como alternativa.",
    ]
    return "\n".join(lines) + "\n"


def _fmt(value: float) -> str:
    return "n/d" if value != value else f"{value:.4f}"


def render(results: list[dict[str, Any]], counts: dict[str, int], test_year: int) -> str:
    first = results[0]["report"]["metrics"]
    lines = [
        "# Avaliação do modelo",
        "",
        f"Gerado por `scripts/train_local.py` em {datetime.now(UTC).date().isoformat()}, "
        f"com teste em {test_year}, validação em {test_year - 1} e treino até {test_year - 2}.",
        f"Linhas: treino {counts['train']}, validação {counts['validation']}, "
        f"teste {counts['test']}. Rótulo: chuva ≥ 0,5 mm na hora que termina em t+h.",
        "",
        "## Resultado no teste",
        "",
        "| Preditor | Brier | Skill vs melhor baseline | PR-AUC | CSI | POD | FAR |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("persistence", "forecast"):
        m = first[name]
        lines.append(
            f"| baseline: {name} | {_fmt(m['brier'])} | — | {_fmt(m['pr_auc'])} "
            f"| {_fmt(m['csi'])} | {_fmt(m['pod'])} | {_fmt(m['far'])} |"
        )
    for result in results:
        report = result["report"]
        m = report["metrics"]["model"]
        lines.append(
            f"| XGBoost {result['feature_set']}, scale_pos_weight={result['scale_pos_weight']} "
            f"| {_fmt(m['brier'])} | {_fmt(report['brier_skill_vs_best_baseline'])} "
            f"| {_fmt(m['pr_auc'])} | {_fmt(m['csi'])} | {_fmt(m['pod'])} | {_fmt(m['far'])} |"
        )

    best = min(results, key=lambda r: r["report"]["metrics"]["model"]["brier"])
    metrics = best["report"]["metrics"]
    lines += [
        "",
        f"## Por horizonte: XGBoost {best['feature_set']}, "
        f"scale_pos_weight={best['scale_pos_weight']}",
        "",
        "| Horizonte | Brier modelo | Brier persistência | Brier previsão "
        "| PR-AUC modelo | CSI modelo |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for horizon, m in metrics["model"]["by_horizon"].items():
        p = metrics["persistence"]["by_horizon"][horizon]
        f = metrics["forecast"]["by_horizon"][horizon]
        lines.append(
            f"| {horizon} h | {_fmt(m['brier'])} | {_fmt(p['brier'])} | {_fmt(f['brier'])} "
            f"| {_fmt(m['pr_auc'])} | {_fmt(m['csi'])} |"
        )
    lines += [
        "",
        "Features com maior ganho: "
        + ", ".join(f"`{name}`" for name, _ in best["top_features"])
        + ".",
        "",
        "## Ressalvas",
        "",
        "- As features `fc_*` e a baseline de previsão usam a Historical Forecast API, que",
        "  junta as primeiras horas de rodadas sucessivas. Para t+3 h e t+6 h, parte desses",
        "  valores vem de rodadas emitidas depois de t, então o conjunto `full` e a baseline de",
        "  previsão são otimistas (ver ADR-009). O conjunto `obs_only` não tem esse viés.",
        "- `scale_pos_weight` maior que 1 desloca as probabilidades para cima e piora o Brier;",
        "  ele só aparece aqui como comparação pedida no plano.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-year", type=int, default=DEFAULT_TEST_YEAR)
    parser.add_argument("--skip-sync", action="store_true")
    parser.add_argument("--profile")
    args = parser.parse_args(argv)

    try:
        if not args.skip_sync:
            session = boto3.Session(profile_name=args.profile, region_name=REGION)
            account = session.client("sts").get_caller_identity()["Account"]
            count = sync(session.client("s3"), f"chuva-bh-curated-{account}-{REGION}", CACHE_DIR)
            logger.info("Objetos baixados: %d", count)
    except (ClientError, BotoCoreError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1

    observations = read_partitioned(CACHE_DIR / "observations")
    forecasts = read_partitioned(CACHE_DIR / "forecasts")
    metar = read_partitioned(CACHE_DIR / "metar")
    _, split = prepare(observations, forecasts, args.test_year)
    counts = {n: len(getattr(split, n)) for n in ("train", "validation", "test")}
    results = run_experiments(split.train, split.validation, split.test)

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(results, default=float), encoding="utf-8")
    # Modo METAR: só a Pampulha e só o período com METAR, para todos no mesmo conjunto.
    _, metar_split = prepare(observations, forecasts, args.test_year, metar, "metar")
    metar_counts = {n: len(getattr(metar_split, n)) for n in ("train", "validation", "test")}
    metar_results = run_experiments(
        metar_split.train,
        metar_split.validation,
        metar_split.test,
        feature_sets=("metar", "full", "obs_only"),
        compare_weights=False,
    )
    report = render(results, counts, args.test_year) + render_metar(metar_results, metar_counts)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(f"Relatório gravado em {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
