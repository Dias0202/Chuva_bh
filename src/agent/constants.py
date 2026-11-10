"""Constantes do agente, sem dependências: usadas também pelo CDK."""

from __future__ import annotations

from pathlib import Path

# Perfil de inferência entre regiões dos EUA. Ver ADR-011 sobre a SCP que nega us-east-1.
DEFAULT_MODEL_ID = "us.amazon.nova-lite-v1:0"
PROMPT_FILE = Path(__file__).with_name("system_prompt.md")
DISCLAIMER = "Previsão experimental: não substitui os alertas oficiais da Defesa Civil e do INMET."
