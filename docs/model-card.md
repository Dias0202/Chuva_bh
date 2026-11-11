# Model card: chuva-bh

## Uso pretendido

Estimar a probabilidade de chover pelo menos 0,5 mm na hora que termina 1, 3 ou 6 horas depois
da última observação, em quatro estações automáticas do INMET na região de Belo Horizonte:
Pampulha (A521), Santo Agostinho (A572), Florestal (A535) e Sete Lagoas (A569).

O uso pretendido é informativo: planejar atividades ao ar livre, deslocamentos e trabalho
exposto ao tempo nas próximas horas. Não serve para decisões de segurança e não substitui os
alertas da Defesa Civil nem os avisos do INMET.

## Dados

- **Observação (rótulo e features):** arquivos anuais das estações automáticas do INMET, de 2019
  a outubro de 2026. Cobertura de precipitação de 99 a 100% na Pampulha; Florestal e Sete Lagoas
  têm anos com 71 a 74%.
- **Previsão numérica (features e baseline):** Historical Forecast API da Open-Meteo, desde
  2021-03-24, sob licença CC BY 4.0.
- **Qualidade:** faixas físicas, chaves duplicadas e lacunas verificadas em toda carga
  (`docs/data-quality.md`).

## Modelo

- XGBoost (`binary:logistic`), um único modelo com o horizonte como feature.
- Features: chuva atual e acumulada em 3, 6 e 24 h, temperatura, ponto de orvalho, umidade,
  tendência de pressão, vento, radiação, hora e dia do ano, e a chuva, CAPE, nuvens e umidade
  previstas pela Open-Meteo para o horizonte.
- Split temporal: treino até 2023, validação em 2024 e teste em 2025, com descarte das horas
  de fronteira. Os limiares de CSI são escolhidos na validação.

## Resultado no teste (2025)

| Preditor | Brier | PR-AUC | CSI | POD | FAR |
|---|---:|---:|---:|---:|---:|
| Persistência calibrada | 0,0239 | 0,159 | 0,176 | 0,300 | 0,700 |
| Previsão Open-Meteo calibrada | 0,0242 | 0,157 | 0,177 | 0,427 | 0,769 |
| XGBoost, todas as features | **0,0211** | **0,367** | **0,248** | 0,490 | 0,666 |
| XGBoost, só observações | 0,0232 | 0,271 | 0,174 | 0,375 | 0,754 |

Por horizonte, o modelo com todas as features tem Brier de 0,0180 em 1 h, 0,0224 em 3 h e
0,0229 em 6 h, sempre abaixo das duas referências. Detalhes em `docs/evaluation.md`.

O pipeline do SageMaker reproduziu esses números em 2026-11-10 (Brier 0,0211, PR-AUC 0,371) e
registrou a versão 1 no grupo `chuva-bh-rain`, pendente de aprovação manual.

## Modo METAR (versão 3, em produção)

Sem a chave da API do INMET, o modelo em produção usa o METAR do aeroporto da Pampulha no
lugar da observação da estação: temperatura, umidade, pressão, vento, teto de nuvens e se está
chovendo, mais a previsão numérica. Ele só cobre o ponto A521. No teste de 2025, teve Brier de
0,0225 (11,3% melhor que a melhor baseline) e PR-AUC de 0,357, perto do modelo completo. O
aeroporto não emite METAR de madrugada, e nessas horas o modelo depende da previsão numérica
(ADR-013).

## Tuning

Um tuning Bayesiano com 10 jobs Spot (2 em paralelo) chegou a logloss de validação de 0,08753,
contra 0,08778 dos hiperparâmetros padrão: 0,3% de melhora, dentro do ruído. O padrão foi
mantido. Melhor combinação: `eta` 0,072, `max_depth` 4, `min_child_weight` 20 e `subsample` 0,90.

## Explicabilidade

Valores SHAP do modelo registrado, calculados com TreeSHAP, estão em `docs/explainability.md`.
As features de previsão numérica (nuvens, chuva acumulada e umidade previstas) dominam no
geral; nas horas com chuva, a chuva observada no momento ganha peso.

## Limitações e vieses

- **Vazamento parcial nas features de previsão.** A série histórica da Open-Meteo não informa a
  rodada de origem, então parte dos valores para 3 e 6 h vem de rodadas posteriores ao instante
  da previsão. O resultado com todas as features é otimista; o modelo só com observações não tem
  esse problema (ADR-009).
- **Um ano de teste.** 2025 pode não representar anos mais chuvosos ou mais secos.
- **Evento raro.** Só cerca de 3% das horas têm chuva acima do limiar. O Brier baixo reflete
  isso; PR-AUC e CSI mostram melhor a capacidade de acertar as horas de chuva.
- **Representação espacial.** Quatro estações, uma delas com pouco mais de um ano de dados.
  Pancadas de verão são localizadas, e o resultado na estação pode não valer para bairros
  vizinhos.
- **Calibração.** `scale_pos_weight` maior que 1 desloca as probabilidades para cima e piora o
  Brier, por isso o modelo usa peso 1.
- **Dependência de dados ao vivo.** Sem a chave da API do INMET, a observação chega com cerca de
  um mês de atraso, e o scoring em produção não roda.

## Monitoramento

Brier do modelo contra a persistência em 7 e 30 dias, PSI das features observadas contra o
período de treino e alarme que inicia o retreino, com aprovação manual mantida (ADR-012).
