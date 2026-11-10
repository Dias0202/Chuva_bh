# Arquitetura

Tudo roda em `us-east-2`, sem VPC permanente nem NAT, e é criado pelo CDK em seis stacks:
`foundation`, `ingestion`, `ml`, `serving`, `genai` e `observability`.

```mermaid
flowchart LR
  subgraph D1[Dados]
    SCH1[Scheduler<br/>minuto 5] --> ING[Lambda ingest]
    OM[Open-Meteo] --> ING
    INMETAPI[API INMET<br/>com chave] -.-> ING
    ING --> RAW[(S3 raw)]
    ING --> CUR[(S3 curated<br/>Parquet)]
    BULK[ZIPs anuais INMET] --> BF[backfill.py] --> CUR
    CUR --> GLUE[Glue Catalog<br/>partition projection] --> ATH[Athena]
  end

  subgraph D2[Treino]
    CUR --> PIPE[SageMaker Pipeline<br/>Processing, Training Spot,<br/>Evaluation, Condition]
    PIPE --> REG[Model Registry<br/>aprovação manual]
  end

  subgraph D3[Serving]
    REG -->|aprovado| DEP[Lambda deployer] --> EP[Serverless Endpoint]
    SCH2[Scheduler<br/>minuto 20] --> SCORE[Lambda score] --> EP
    SCORE --> DDB[(DynamoDB<br/>previsões, TTL 90 dias)]
    API[HTTP API<br/>IAM e throttling] --> LAPI[Lambda API] --> DDB
  end

  subgraph GENAI[Agente]
    USER[CLI] --> RT[AgentCore Runtime<br/>LangGraph]
    RT -->|SigV4| API
    RT --> KB[Knowledge Base<br/>S3 Vectors]
    RT --> BR[Bedrock Nova Lite<br/>Guardrail e prompt versionado]
  end

  subgraph D4[Operação]
    Q[Lambda quality<br/>a cada hora] --> CW[CloudWatch<br/>métricas e dashboard]
    DR[Lambda drift<br/>diária] --> CW
    DDB --> Q
    CUR --> Q
    CUR --> DR
    CW --> SNS[SNS e-mail]
    CW -->|Brier pior por 7 dias| PIPE
    CT[CloudTrail]
    BUD[Budget e kill switch]
  end
```

## Fluxo de uma previsão

1. No minuto 5 de cada hora, a ingestão grava a rodada da Open-Meteo e, com a chave do INMET, a
   observação da hora. Os objetos têm chave determinística, então uma nova tentativa sobrescreve.
2. No minuto 20, o scoring lê a configuração do modelo ativo no SSM, monta as features da hora,
   chama o endpoint e grava uma linha por horizonte no DynamoDB.
3. O agente consulta a API assinada com SigV4 e responde em hora de Brasília.
4. No minuto 40, a Lambda de qualidade compara as previsões já vencidas com a chuva observada.

## Decisões

As decisões estão em `docs/adr/`. As principais: Serverless Inference (ADR-001), S3 Vectors em
vez de OpenSearch Serverless (ADR-002), ground truth nas estações do INMET (ADR-006), região
us-east-2 por causa da SCP da conta (ADR-007) e o vazamento parcial nas features de previsão
histórica (ADR-009).
