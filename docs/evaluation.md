# Avaliação do modelo

Gerado por `scripts/train_local.py` em 2026-11-10, com teste em 2025, validação em 2024 e treino até 2023.
Linhas: treino 201729, validação 72666, teste 87349. Rótulo: chuva ≥ 0,5 mm na hora que termina em t+h.

## Resultado no teste

| Preditor | Brier | Skill vs melhor baseline | PR-AUC | CSI | POD | FAR |
|---|---:|---:|---:|---:|---:|---:|
| baseline: persistence | 0.0239 | — | 0.1590 | 0.1763 | 0.2997 | 0.7003 |
| baseline: forecast | 0.0242 | — | 0.1567 | 0.1765 | 0.4270 | 0.7687 |
| XGBoost full, scale_pos_weight=1 | 0.0211 | 0.1183 | 0.3674 | 0.2482 | 0.4903 | 0.6655 |
| XGBoost full, scale_pos_weight=auto | 0.0677 | -1.8296 | 0.3435 | 0.2302 | 0.5059 | 0.7030 |
| XGBoost obs_only, scale_pos_weight=1 | 0.0232 | 0.0280 | 0.2708 | 0.1743 | 0.3745 | 0.7541 |
| XGBoost obs_only, scale_pos_weight=auto | 0.0942 | -2.9381 | 0.2476 | 0.1669 | 0.4060 | 0.7792 |

## Por horizonte: XGBoost full, scale_pos_weight=1

| Horizonte | Brier modelo | Brier persistência | Brier previsão | PR-AUC modelo | CSI modelo |
|---|---:|---:|---:|---:|---:|
| 1 h | 0.0180 | 0.0201 | 0.0243 | 0.5168 | 0.3408 |
| 3 h | 0.0224 | 0.0252 | 0.0242 | 0.2802 | 0.2075 |
| 6 h | 0.0229 | 0.0265 | 0.0242 | 0.2537 | 0.2114 |

Features com maior ganho: `fc_precipitation_sum_mm`, `fc_precipitation_mm`, `precip_mm`, `precip_sum_3h_mm`, `fc_cloud_cover_pct`, `fc_relative_humidity_pct`, `horizon_h`, `precip_sum_6h_mm`.

## Ressalvas

- As features `fc_*` e a baseline de previsão usam a Historical Forecast API, que junta
  as primeiras horas de rodadas sucessivas. Para t+3 h e t+6 h, parte desses valores vem de
  rodadas emitidas depois de t, então o conjunto `full` e a baseline de previsão são
  otimistas (ver ADR-009). O conjunto `obs_only` não tem esse viés.
- `scale_pos_weight` maior que 1 desloca as probabilidades para cima e piora o Brier;
  ele só aparece aqui como comparação pedida no plano.
