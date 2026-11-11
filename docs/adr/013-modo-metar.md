# ADR-013: modo METAR para previsão ao vivo na Pampulha

Status: aceito em 2026-11-11.

## Contexto

O scoring precisa da observação da hora, e a API do INMET exige uma chave que ainda não
chegou. Sem ela, nenhum ponto tinha previsão ao vivo. O METAR do aeroporto da Pampulha
(SBBH) é público, horário e não exige credencial, mas não informa a chuva em mm: só se está
chovendo ou trovejando. O CEMADEN, que mede chuva em mm, também exige cadastro.

## Decisão

- Novo dataset `metar`, alimentado a cada hora pela API do aviationweather.gov e, no
  histórico, pelo arquivo do Iowa Environmental Mesonet (2019 em diante). O METAR é associado
  ao ponto A521, a cerca de 3 km da pista.
- Novo conjunto de features `metar`: temperatura, ponto de orvalho, umidade, pressão ao nível
  do mar e tendência, vento, teto de nuvens, chuva e trovoada na hora e horas com chuva nas
  últimas 3 e 6 horas, mais calendário e previsão numérica. O rótulo continua sendo a chuva
  medida pela estação A521 do INMET.
- Mesmo grupo do Model Registry e mesmo endpoint. O modelo ativo define o conjunto de features
  pelo parâmetro do SSM, e o scoring monta as features de acordo. A versão 3 (METAR) foi
  aprovada e substituiu a versão 1 no endpoint.

## Resultado

No teste de 2025, só na Pampulha e só no período com METAR:

| Preditor | Brier | PR-AUC |
|---|---:|---:|
| Persistência calibrada | 0,0255 | 0,155 |
| Previsão Open-Meteo calibrada | 0,0253 | 0,162 |
| XGBoost METAR (versão 3, SageMaker) | **0,0225** | **0,357** |
| XGBoost completo, com INMET (treino local) | 0,0222 | 0,370 |

## Consequências

- Só a Pampulha tem previsão ao vivo. Os outros três pontos ficam sem previsão até a chave do
  INMET chegar.
- O aeroporto não emite METAR de madrugada; nessas horas as features do METAR ficam vazias,
  tanto no treino quanto no scoring, e o modelo se apoia na previsão numérica.
- A medição de qualidade continua dependendo dos arquivos mensais do INMET.
- Quando a chave chegar, basta aprovar de novo um modelo completo: o deployer troca o endpoint
  e o scoring volta a cobrir os quatro pontos.
