# ADR-012: monitoramento de qualidade e drift

Status: aceito em 2026-11-10.

## Decisões

- **Qualidade contra a observação.** A Lambda `quality` roda a cada hora e junta as previsões do
  DynamoDB com a chuva observada no data lake. Ela publica Brier do modelo e da referência em
  janelas de 7 e 30 dias, e PR-AUC e CSI (limiar 0,3) na janela de 30 dias.
- **Referência calculada pelos próprios dados.** A persistência é calibrada no ano anterior à
  janela, com a mesma classe usada no treino (`PersistenceBaseline`). Assim o monitoramento não
  depende de artefatos do treino.
- **Drift das features observadas.** A Lambda `drift` roda uma vez por dia e compara a última
  semana com o período de treino (2021-03-24 a 2023), em 10 faixas por quantis. A referência é
  calculada na primeira execução e guardada em `monitoring/drift-reference.json`.
- **Retreino por alarme.** O alarme "Brier pior que a persistência por 7 dias" inicia o
  pipeline com `ModelApprovalStatus=PendingManualApproval`, então um modelo novo nunca entra
  em produção sem aprovação.
- **Model Monitor à parte.** Como o Serverless Inference não tem data capture (ADR-003), o
  exercício usa um endpoint real-time temporário, com a tag `TTL` e o `ttl_sweeper.py`.

## Custo

Cada combinação de nome e dimensão é uma métrica customizada (US$ 0,30 por mês acima das 10
gratuitas). Para conter o custo, o drift publica `MaxPsi` e só três features com dimensão
própria; as demais ficam no log. Com todas as fases no ar, são cerca de 27 métricas, ou
US$ 5 por mês.

## Limitações conhecidas

- Sem a chave da API do INMET, não há previsões nem observações recentes, e as duas Lambdas só
  registram que não havia amostras.
- O tópico SNS não usa criptografia com CMK: alarmes do CloudWatch não publicam em tópicos com a
  chave gerenciada pela AWS, e as mensagens não têm dados sensíveis.
- O kill switch da Fase 0 nega `StartPipelineExecution` só para a role de execução do CDK. A
  role do gatilho de retreino não está coberta e deve entrar no kill switch antes do deploy
  desta stack.
