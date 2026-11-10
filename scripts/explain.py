#!/usr/bin/env python
"""Explicabilidade do modelo registrado com TreeSHAP nativo do XGBoost.

Substitui o job do Clarify, que está em modo de manutenção para contas novas. Baixa o
artefato do pacote do Model Registry e o conjunto de teste do pipeline, calcula as
contribuições SHAP exatas das árvores e grava docs/explainability.md.

Uso:
    explain.py --model-package-arn ARN --test S3_URI_DO_TESTE

Só lê da AWS.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import boto3
import numpy as np
import pandas as pd
import xgboost as xgb
from botocore.exceptions import BotoCoreError, ClientError

ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from features.build import FEATURE_SETS, LABEL  # noqa: E402
from training.evaluate import MODEL_FILE, load_booster  # noqa: E402

REGION: Final = "us-east-2"
LEGACY_XGBOOST: Final = "xgboost==3.0.5"
WORK_DIR: Final = ROOT / ".cache" / "explain"
REPORT_PATH: Final = ROOT / "docs" / "explainability.md"
SAMPLE_ROWS: Final = 20000
TOP_FEATURES: Final = 15


def _split(uri: str) -> tuple[str, str]:
    bucket, _, key = uri.removeprefix("s3://").partition("/")
    return bucket, key


def convert_legacy_model(path: Path) -> Path:
    """Converte o formato binário antigo do container 1.7-1 para JSON.

    O XGBoost 3.1 removeu a leitura desse formato. A conversão usa o XGBoost 3.0 num
    ambiente temporário do uv, sem mudar as dependências do projeto.
    """
    target = path.with_suffix(".json")
    code = (
        "import sys, xgboost as x; b = x.Booster(); "
        "b.load_model(sys.argv[1]); b.save_model(sys.argv[2])"
    )
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv não encontrado no PATH para converter o modelo")
    subprocess.run(  # noqa: S603 - argumentos fixos, sem shell
        [
            uv,
            "run",
            "--no-project",
            "--quiet",
            "--with",
            LEGACY_XGBOOST,
            "python",
            "-c",
            code,
            str(path),
            str(target),
        ],
        check=True,
    )
    return target


def download_model(session: boto3.Session, package_arn: str) -> tuple[xgb.Booster, str]:
    package = session.client("sagemaker").describe_model_package(ModelPackageName=package_arn)
    url = package["InferenceSpecification"]["Containers"][0]["ModelDataUrl"]
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    bucket, key = _split(url)
    session.client("s3").download_file(bucket, key, str(WORK_DIR / "model.tar.gz"))
    try:
        booster = load_booster(WORK_DIR)
    except xgb.core.XGBoostError:
        booster = xgb.Booster()
        booster.load_model(str(convert_legacy_model(WORK_DIR / MODEL_FILE)))
    assert isinstance(booster, xgb.Booster)  # noqa: S101
    return booster, str(package["ModelPackageVersion"])


def download_test(session: boto3.Session, uri: str) -> pd.DataFrame:
    bucket, prefix = _split(uri.rstrip("/"))
    target = WORK_DIR / "test.parquet"
    session.client("s3").download_file(bucket, f"{prefix}/test.parquet", str(target))
    return pd.read_parquet(target)


def mean_abs_shap(booster: xgb.Booster, frame: pd.DataFrame, features: list[str]) -> pd.Series:
    matrix = xgb.DMatrix(frame[features].astype(np.float32), missing=np.nan)
    contributions = booster.predict(matrix, pred_contribs=True)
    # A última coluna é o termo de viés, que não pertence a nenhuma feature.
    values = np.abs(contributions[:, :-1]).mean(axis=0)
    return pd.Series(values, index=features).sort_values(ascending=False)


def render(overall: pd.Series, rainy: pd.Series, version: str, rows: int) -> str:
    lines = [
        "# Explicabilidade do modelo",
        "",
        f"Gerado por `scripts/explain.py` em {datetime.now(UTC).date().isoformat()}, para a versão "
        f"{version} do grupo `chuva-bh-rain`, com {rows} linhas do teste de 2025.",
        "",
        "Valores SHAP exatos (TreeSHAP do XGBoost), na escala de log-odds. Quanto maior a média do",
        "valor absoluto, mais a feature move a probabilidade prevista.",
        "",
        "| Feature | Média de \\|SHAP\\| (todas as horas) "
        "| Média de \\|SHAP\\| (horas com chuva) |",
        "|---|---:|---:|",
    ]
    for feature in overall.index[:TOP_FEATURES]:
        lines.append(f"| `{feature}` | {overall[feature]:.4f} | {rainy[feature]:.4f} |")
    lines += [
        "",
        "O Clarify do SageMaker foi o plano original, mas o processing dele está em modo de",
        "manutenção para contas novas. O TreeSHAP calcula os mesmos valores de Shapley sem",
        "amostragem, porque usa a estrutura das árvores.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-package-arn", required=True)
    parser.add_argument("--test", required=True, help="URI S3 da saída test do Preprocess")
    parser.add_argument("--feature-set", choices=sorted(FEATURE_SETS), default="full")
    parser.add_argument("--profile")
    args = parser.parse_args(argv)

    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    try:
        booster, version = download_model(session, args.model_package_arn)
        test = download_test(session, args.test)
    except (ClientError, BotoCoreError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1

    features = list(FEATURE_SETS[args.feature_set])
    sample = test.sample(n=min(SAMPLE_ROWS, len(test)), random_state=42)
    overall = mean_abs_shap(booster, sample, features)
    rainy = mean_abs_shap(booster, test[test[LABEL] == 1], features).reindex(overall.index)
    REPORT_PATH.write_text(render(overall, rainy, version, len(sample)), encoding="utf-8")
    print(f"Relatório gravado em {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
