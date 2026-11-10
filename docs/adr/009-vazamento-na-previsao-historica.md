# ADR-009: vazamento temporal nas features de previsão histórica

Status: aceito em 2026-11-10.

## Contexto

A Historical Forecast API da Open-Meteo devolve uma série contínua montada com as primeiras
horas de rodadas sucessivas, sem informar a hora de emissão. Para prever t+h a partir de t, o
valor previsto para t+h pode ter vindo de uma rodada emitida depois de t. Esse valor não estaria
disponível em produção.

## Decisão

- Treinar e reportar dois conjuntos de features: `full`, com as features `fc_*`, e `obs_only`,
  só com observações e calendário.
- O pipeline usa `full` por padrão, e a baseline de previsão usa a mesma série. O modelo e a
  baseline recebem a mesma vantagem, então a comparação entre eles continua válida, mas os
  números absolutos são otimistas.
- A ingestão horária grava cada rodada com `issued_at`. Quando houver alguns meses dessa série,
  o retreino deve usar só rodadas com `issued_at <= t`, o que elimina o vazamento.

## Resultado do treino local (teste em 2025)

- `full`: Brier 11,8% melhor que a melhor baseline.
- `obs_only`: Brier 2,8% melhor que a melhor baseline, sem vazamento.

Os números completos estão em `docs/evaluation.md`.

## Alternativa considerada

A Previous Runs API da Open-Meteo, segundo a documentação, oferece previsões com antecedência
fixa em dias, o que não cobre horizontes de 1 a 6 h. Ela não foi testada e pode virar uma feature
adicional, sem resolver o caso.
