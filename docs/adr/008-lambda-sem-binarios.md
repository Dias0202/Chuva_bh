# ADR-008: Lambda de ingestão sem empacotar binários

Status: aceito em 2026-11-10.

## Contexto

A Lambda precisa escrever Parquet, e o `pyarrow` é um pacote nativo para Linux. O deploy é feito
de uma estação Windows, sem Docker, e o CI só roda `cdk synth`.

## Decisão

- Usar a layer pública do AWS SDK for pandas para Python 3.12, que inclui o `pyarrow` 24.
  A versão em `pyproject.toml` é a mesma da layer.
- O pacote da função contém apenas código Python puro de `src/`.
- Validar com dataclasses e `ingestion.quality`, em vez de `pydantic` ou `pandera`, que exigiriam
  empacotar binários ou trariam um passo de build antes do `cdk synth`.

## Consequências

- A versão da layer está fixada em `infra/stacks/ingestion_stack.py` e precisa ser atualizada
  junto com o `pyarrow`.
- A API da Fase 3 ainda exige `pydantic` (seção 3.2 do plano). Essa fase precisará empacotar
  dependências nativas.
