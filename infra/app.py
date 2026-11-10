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
from stacks.genai_stack import GenAiStack
from stacks.ingestion_stack import IngestionStack
from stacks.ml_stack import MlStack
from stacks.observability_stack import ObservabilityStack
from stacks.serving_stack import ServingStack

ENV_BUDGET_EMAIL: Final = "CHUVA_BUDGET_EMAIL"
ENV_KILL_SWITCH_USERS: Final = "CHUVA_KILL_SWITCH_USERS"
ENV_KILL_SWITCH_ROLES: Final = "CHUVA_KILL_SWITCH_ROLES"


def _split(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def load_config(environ: Mapping[str, str]) -> FoundationConfig:
    """Lê a configuração do ambiente, sem valor padrão para o e-mail de alertas.

    Não há fallback de propósito: um e-mail de exemplo implantado por engano
    silenciaria todos os alertas de custo.
    """
    if not environ.get(ENV_BUDGET_EMAIL):
        raise SystemExit(f"Variável de ambiente obrigatória ausente: {ENV_BUDGET_EMAIL}")

    return FoundationConfig(
        budget_email=environ[ENV_BUDGET_EMAIL],
        kill_switch_users=_split(environ.get(ENV_KILL_SWITCH_USERS, "")),
        kill_switch_roles=_split(environ.get(ENV_KILL_SWITCH_ROLES, "")),
    )


def build_app(environ: Mapping[str, str], context: Mapping[str, Any] | None = None) -> App:
    """Monta o app com todas as stacks do projeto."""
    app = App(context=dict(context) if context else None)
    env = Environment(account=environ.get("CDK_DEFAULT_ACCOUNT"), region=REGION)
    config = load_config(environ)
    foundation = FoundationStack(
        app,
        "FoundationStack",
        stack_name=f"{PROJECT}-foundation",
        description="chuva-bh: base de criptografia, dados, auditoria e custo",
        env=env,
        config=config,
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
    serving = ServingStack(
        app,
        "ServingStack",
        stack_name=f"{PROJECT}-serving",
        description="chuva-bh: scoring horário, API de previsões e deploy do endpoint",
        env=env,
        curated_bucket=foundation.buckets["curated"],
        sagemaker_role=ml.role,
    )
    GenAiStack(
        app,
        "GenAiStack",
        stack_name=f"{PROJECT}-genai",
        description="chuva-bh: Knowledge Base, Guardrail, prompt e agente no AgentCore",
        env=env,
        kb_bucket=foundation.buckets["kb-docs"],
        key=foundation.key,
        api=serving.api,
        chunking=app.node.try_get_context("kb_chunking") or "fixed",
    )
    ObservabilityStack(
        app,
        "ObservabilityStack",
        stack_name=f"{PROJECT}-observability",
        description="chuva-bh: qualidade, drift, dashboard, alarmes e retreino",
        env=env,
        curated_bucket=foundation.buckets["curated"],
        artifacts_bucket=foundation.buckets["artifacts"],
        table=serving.table,
        api=serving.api,
        alert_email=config.budget_email,
    )
    return app


if __name__ == "__main__":
    build_app(os.environ).synth()
