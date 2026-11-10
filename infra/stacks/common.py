"""Convenções compartilhadas entre as stacks do projeto."""

from __future__ import annotations

from typing import Final

from aws_cdk import Tags
from constructs import IConstruct

PROJECT: Final = "chuva-bh"
OWNER: Final = "dias"
REGION: Final = "us-east-1"


def project_tags(phase: str) -> dict[str, str]:
    """Retorna as tags obrigatórias de todo recurso do projeto."""
    return {"Project": PROJECT, "Phase": phase, "Owner": OWNER}


def apply_project_tags(scope: IConstruct, phase: str) -> None:
    """Aplica as tags obrigatórias a um construct e a todos os seus filhos."""
    for key, value in project_tags(phase).items():
        Tags.of(scope).add(key, value)
