# ADR-006: fontes de observação e de previsão

Status: aceito em 2026-11-10, com pendência sobre a observação quase em tempo real.

## Contexto

O modelo precisa ser avaliado contra chuva observada em estação ou pluviômetro. Reanálise e
previsão não são observação, e avaliar contra elas seria circular.

## O que foi verificado

| Fonte | Acesso sem credencial | Resultado |
|---|---|---|
| INMET, arquivos anuais em ZIP | Sim | Dados horários por estação, publicados com cerca de um mês de atraso: em 2026-11-10 o arquivo do ano ia até 2026-10-31 |
| INMET, API horária (`apitempo.inmet.gov.br/estacao/...`) | Não | HTTP 204 em todas as consultas. A rota com chave responde `CHAVE INVÁLIDA` |
| CEMADEN, API de dados (`sws.cemaden.gov.br/PED`) | Não | HTTP 403 |
| CEMADEN, mapa interativo | Parcial | Resposta sem estação para BH |
| Open-Meteo Forecast e Historical Forecast | Sim | Respondem normalmente. Dados CC BY 4.0, uso não comercial, até 10.000 chamadas por dia |
| METAR de SBBH (`aviationweather.gov`) | Sim | Horário e sem chave, mas informa o tempo presente, não a precipitação em mm |

Cobertura de precipitação nos ZIPs do INMET, de 2019 a outubro de 2026:

| Estação | Situação | Cobertura anual |
|---|---|---|
| A521 Pampulha | Operante | 99,0% a 100% |
| A572 Santo Agostinho | Operante desde 2025-08 | 95,7% a 97,6% |
| A535 Florestal | Operante | 74,0% a 100% |
| A569 Sete Lagoas | Operante | 71,0% a 100% |
| A555 Ibirité (Rola Moça) | Em pane | Sem dados em 2026, descartada |

A estação F501 (Cercadinho) não foi avaliada. A licença dos arquivos do INMET não consta neles
e precisa ser confirmada antes de redistribuir dados derivados.

## Decisão

- Ground truth: precipitação das estações A521, A572, A535 e A569, a partir dos ZIPs do INMET.
- Variáveis e baseline: Forecast API e Historical Forecast API da Open-Meteo. A Archive API
  (ERA5) não é usada, pois é reanálise.
- Janela: observações desde 2019 e previsões desde 2021-03-24, data a partir da qual o histórico
  tem CAPE.

## Consequências

- Sem chave da API do INMET, a observação chega com cerca de um mês de atraso. A medição de
  qualidade da Fase 5 fica defasada nesse período.
- A carga mensal é feita com `backfill.py --dataset observations --start-year <ano>`.
- A Historical Forecast API não informa a rodada de origem, e `precipitation_probability` é nula
  no início da série (ao menos até meados de 2022). A série junta as primeiras horas de rodadas
  sucessivas, então representa horizontes curtos. O baseline histórico usa a precipitação prevista,
  e os horizontes de 3 h e 6 h precisam dessa ressalva no model card.
- Para o ciclo fechado em tempo quase real, a saída é pedir a chave ao INMET. O METAR de SBBH é
  uma alternativa parcial: indica se choveu, sem o volume e só para a Pampulha.
