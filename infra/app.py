"""Ponto de entrada do CDK."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from aws_cdk import App, Environment

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stacks.common import PROJECT, REGION
from stacks.foundation_stack import FoundationConfig, FoundationStack
from stacks.ingestion_stack import IngestionStack
from stacks.ml_stack import MlStack
from stacks.serving_stack import ServingStack

ENV_BUDGET_EMAIL: Final = "CHUVA_BUDGET_EMAIL"
ENV_KILL_SWITCH_USERS: Final = "CHUVA_KILL_SWITCH_USERS"


def load_config(environ: Mapping[str, str]) -> FoundationConfig:
    """Lê a configuração do ambiente, sem valor padrão para o e-mail de alertas.

    Não há fallback de propósito: um e-mail de exemplo implantado por engano
    silenciaria todos os alertas de custo.
    """
    if not environ.get(ENV_BUDGET_EMAIL):
        raise SystemExit(f"Variável de ambiente obrigatória ausente: {ENV_BUDGET_EMAIL}")

    users = tuple(
        user.strip() for user in environ.get(ENV_KILL_SWITCH_USERS, "").split(",") if user.strip()
    )
    return FoundationConfig(
        budget_email=environ[ENV_BUDGET_EMAIL],
        kill_switch_users=users,
    )


def build_app(environ: Mapping[str, str], context: Mapping[str, Any] | None = None) -> App:
    """Monta o app com todas as stacks do projeto."""
    app = App(context=dict(context) if context else None)
    env = Environment(account=environ.get("CDK_DEFAULT_ACCOUNT"), region=REGION)
    foundation = FoundationStack(
        app,
        "FoundationStack",
        stack_name=f"{PROJECT}-foundation",
        description="chuva-bh: base de criptografia, dados, auditoria e custo",
        env=env,
        config=load_config(environ),
    )
    IngestionStack(
        app,
        "IngestionStack",
        stack_name=f"{PROJECT}-ingestion",
        description="chuva-bh: ingestão horária, catálogo Glue e consultas Athena",
        env=env,
        raw_bucket=foundation.buckets["raw"],
        curated_bucket=foundation.buckets["curated"],
        artifacts_bucket=foundation.buckets["artifacts"],
        key=foundation.key,
    )
    ml = MlStack(
        app,
        "MlStack",
        stack_name=f"{PROJECT}-ml",
        description="chuva-bh: role do SageMaker, Model Registry e rede isolada opcional",
        env=env,
        curated_bucket=foundation.buckets["curated"],
        artifacts_bucket=foundation.buckets["artifacts"],
        key=foundation.key,
        isolation=app.node.try_get_context("isolation") == "true",
    )
    ServingStack(
        app,
        "ServingStack",
        stack_name=f"{PROJECT}-serving",
        description="chuva-bh: scoring horário, API de previsões e deploy do endpoint",
        env=env,
        curated_bucket=foundation.buckets["curated"],
        sagemaker_role=ml.role,
    )
    return app


if __name__ == "__main__":
    build_app(os.environ).synth()
