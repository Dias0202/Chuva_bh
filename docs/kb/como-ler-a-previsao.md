# Como ler a previsão do chuva-bh

Texto próprio do projeto, escrito para a base de conhecimento do assistente.

## O que o sistema entrega

Para cada ponto, a probabilidade de chover pelo menos 0,5 mm em três horas específicas: 1, 3 e
6 horas depois da emissão. A previsão é recalculada a cada hora, alguns minutos depois que
chegam a observação mais recente e a nova rodada da previsão numérica.

## A que hora cada número se refere

Cada probabilidade vale para uma hora fechada, a que termina no horário indicado. Numa
previsão emitida às 10h de Brasília:

- horizonte de 1 hora: chuva entre 10h e 11h;
- horizonte de 3 horas: chuva entre 12h e 13h;
- horizonte de 6 horas: chuva entre 15h e 16h.

Os números não são um acumulado. A chance de chover em algum momento das próximas seis horas é
maior que qualquer um deles, e as horas intermediárias não são previstas.

## Como comparar com o normal

Chover numa hora qualquer é raro. Na Pampulha, cerca de 7% das horas de novembro têm chuva de
pelo menos 0,5 mm. Pela manhã são de 2% a 3%; entre 18h e 23h, de 10% a 11%, por causa das
pancadas do fim da tarde. Uma previsão abaixo desses valores indica tempo mais seco que o
habitual para a hora; acima, mais úmido.

## Quanto as probabilidades valem

No ano de teste, quando o modelo disse cerca de 25%, choveu em 19% das vezes; quando disse
cerca de 35%, choveu em 30%. Usado como alerta a partir de 20%, ele acerta 57% das horas com
chuva, e 7 de cada 10 alertas são falsos.

## De onde vêm os dados

- Chuva observada: estações automáticas do INMET (Pampulha, Santo Agostinho, Florestal e Sete
  Lagoas). É o que o modelo aprende a prever.
- Estado do tempo na hora: a estação do INMET ou, na Pampulha, o boletim METAR do aeroporto,
  que informa temperatura, umidade, pressão, vento, nuvens e se está chovendo.
- Previsão numérica: Open-Meteo, sob licença CC BY 4.0.

## Limitações

- O modelo foi avaliado em um único ano de teste (2025).
- Ele prevê chuva no ponto da estação. Bairros vizinhos podem ter resultado diferente,
  principalmente em pancadas de verão, que são localizadas.
- Os horizontes vão até 6 horas. Além disso, o sistema não tem previsão.
- Hoje só a Pampulha tem previsão ao vivo. Os outros pontos dependem da observação horária das
  estações do INMET.
- O aeroporto não emite METAR de madrugada; nessas horas a previsão da Pampulha depende mais da
  previsão numérica.

## Alertas oficiais

Esta previsão não substitui os alertas da Defesa Civil de Belo Horizonte nem os avisos do
INMET. Em caso de risco de alagamento ou deslizamento, siga sempre as orientações oficiais.
