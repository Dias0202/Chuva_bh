"""Constantes do agente, sem dependências: usadas também pelo CDK."""

from __future__ import annotations

from pathlib import Path

# Modelo na própria região: o perfil entre regiões pode rotear para us-east-1, negada pela SCP
# da conta (ADR-011). Chamada testada em us-east-2 em 2026-11-10.
DEFAULT_MODEL_ID = "amazon.nova-lite-v1:0"
PROMPT_FILE = Path(__file__).with_name("system_prompt.md")
DISCLAIMER = "Previsão experimental: não substitui os alertas oficiais da Defesa Civil e do INMET."
