"""Ponto de entrada do CDK."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

from aws_cdk import App, Environment
from stacks.common import PROJECT, REGION
from stacks.foundation_stack import FoundationConfig, FoundationStack

ENV_BUDGET_EMAIL: Final = "CHUVA_BUDGET_EMAIL"
ENV_GITHUB_REPOSITORY: Final = "CHUVA_GITHUB_REPOSITORY"
ENV_KILL_SWITCH_USERS: Final = "CHUVA_KILL_SWITCH_USERS"


def load_config(environ: Mapping[str, str]) -> FoundationConfig:
    """Lê a configuração do ambiente, sem valores padrão para os campos obrigatórios.

    Não há fallback de propósito: um e-mail de exemplo implantado por engano
    silenciaria todos os alertas de custo.
    """
    missing = [name for name in (ENV_BUDGET_EMAIL, ENV_GITHUB_REPOSITORY) if not environ.get(name)]
    if missing:
        raise SystemExit(f"Variáveis de ambiente obrigatórias ausentes: {', '.join(missing)}")

    users = tuple(
        user.strip() for user in environ.get(ENV_KILL_SWITCH_USERS, "").split(",") if user.strip()
    )
    return FoundationConfig(
        budget_email=environ[ENV_BUDGET_EMAIL],
        github_repository=environ[ENV_GITHUB_REPOSITORY],
        kill_switch_users=users,
    )


def build_app(environ: Mapping[str, str]) -> App:
    """Monta o app com todas as stacks do projeto."""
    app = App()
    FoundationStack(
        app,
        "FoundationStack",
        stack_name=f"{PROJECT}-foundation",
        description="chuva-bh: base de criptografia, dados, auditoria, custo e CI/CD",
        env=Environment(account=environ.get("CDK_DEFAULT_ACCOUNT"), region=REGION),
        config=load_config(environ),
    )
    return app


if __name__ == "__main__":
    build_app(os.environ).synth()
