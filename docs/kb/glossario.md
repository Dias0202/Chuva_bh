# Glossário do chuva-bh

Texto próprio do projeto, escrito para a base de conhecimento do agente.

## Chuva, para o sistema

O sistema considera que choveu em uma hora quando a estação mediu pelo menos 0,5 mm de
precipitação naquela hora. Garoas mais fracas que isso não contam.

## Milímetro de chuva

Um milímetro de chuva equivale a um litro de água por metro quadrado. Uma hora com 0,5 mm é
uma chuva fraca; acima de 10 mm em uma hora, a chuva já é forte.

## Probabilidade de chuva

É a chance estimada de chover pelo menos 0,5 mm na hora indicada, naquele ponto. Uma
probabilidade de 30% significa que, em muitas situações parecidas, choveu em cerca de 3 de
cada 10. Não indica quanto vai chover nem a área atingida.

## Horizonte

Quantas horas à frente a previsão vale. O sistema prevê 1, 3 e 6 horas depois da última
observação. Para períodos mais longos, consulte a previsão do INMET.

## Nowcasting

Previsão de curtíssimo prazo, de minutos a poucas horas, baseada principalmente nas
condições observadas agora. É o tipo de previsão que este sistema faz.

## CAPE

Energia potencial disponível para convecção, medida em J/kg. Indica quanta energia o ar tem
para subir e formar nuvens de tempestade. Valores acima de 1000 J/kg favorecem pancadas fortes
no fim da tarde, comuns no verão de Belo Horizonte.

## Ponto de orvalho

Temperatura em que o ar fica saturado e o vapor começa a condensar. Quanto mais próximo da
temperatura do ar, mais úmido o ar está. A diferença entre os dois é uma das variáveis do
modelo.

## Brier score

Medida de erro de uma previsão probabilística: a média do quadrado da diferença entre a
probabilidade prevista e o que aconteceu (1 se choveu, 0 se não). Quanto menor, melhor. O
projeto compara o Brier do modelo com o de duas referências: a persistência e a previsão
numérica da Open-Meteo.

## Persistência

Referência que assume que o tempo da próxima hora será parecido com o de agora: se está
chovendo, a chance de continuar chovendo é alta.
