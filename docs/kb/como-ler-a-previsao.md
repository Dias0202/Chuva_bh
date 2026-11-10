# Como ler a previsão do chuva-bh

Texto próprio do projeto, escrito para a base de conhecimento do agente.

## O que o sistema entrega

Para cada ponto, a probabilidade de chover pelo menos 0,5 mm em 1, 3 e 6 horas. A previsão é
recalculada a cada hora, alguns minutos depois que chegam a observação da estação e a nova
rodada da previsão numérica.

## De onde vêm os dados

- Chuva observada, temperatura, umidade, pressão e vento: estações automáticas do INMET
  (Pampulha, Santo Agostinho, Florestal e Sete Lagoas).
- Previsão numérica de referência: Open-Meteo, sob licença CC BY 4.0.

## Limitações

- O modelo é experimental e foi avaliado em um único ano de teste (2025).
- Ele prevê chuva no ponto da estação. Bairros vizinhos podem ter resultado diferente,
  principalmente em pancadas de verão, que são localizadas.
- Os horizontes vão até 6 horas. Além disso, o sistema não tem previsão.
- Se a estação parar de enviar dados, o sistema não gera previsão para aquele ponto.
- Parte das variáveis de previsão numérica do histórico de treino tem vazamento temporal
  parcial, o que pode deixar as métricas de 3 e 6 horas otimistas.

## Alertas oficiais

Esta previsão não substitui os alertas da Defesa Civil de Belo Horizonte nem os avisos do
INMET. Em caso de risco de alagamento ou deslizamento, siga sempre as orientações oficiais.
