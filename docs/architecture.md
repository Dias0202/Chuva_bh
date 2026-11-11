# Arquitetura

O sistema roda na AWS, em `us-east-2`, sem servidores permanentes nem rede privada. Toda a
infraestrutura é criada pelo CDK em sete stacks: `foundation`, `ingestion`, `ml`, `serving`,
`genai`, `observability` e `web`.

```mermaid
flowchart LR
  subgraph Dados
    SCH1[Agendamento<br/>minuto 5] --> ING[Lambda de coleta]
    OM[Open-Meteo] --> ING
    MT[METAR do aeroporto] --> ING
    INMETAPI[API horária do INMET] -.-> ING
    ING --> RAW[(S3 bruto)]
    ING --> CUR[(S3 curado<br/>Parquet)]
    BULK[Arquivos anuais do INMET] --> BF[Carga histórica] --> CUR
    CUR --> GLUE[Catálogo Glue] --> ATH[Athena]
  end

  subgraph Treino
    CUR --> PIPE[SageMaker Pipeline<br/>preparação, treino Spot,<br/>avaliação e condição]
    PIPE --> REG[Registro de modelos<br/>aprovação manual]
  end

  subgraph Previsão
    REG -->|aprovado| DEP[Lambda de deploy] --> EP[Endpoint serverless]
    SCH2[Agendamento<br/>minuto 20] --> SCORE[Lambda de previsão] --> EP
    SCORE --> DDB[(DynamoDB<br/>previsões, 90 dias)]
    API[HTTP API<br/>IAM e limite de taxa] --> LAPI[Lambda da API] --> DDB
  end

  subgraph Assistente
    RT[AgentCore Runtime<br/>LangGraph] -->|SigV4| API
    RT --> KB[Base de conhecimento<br/>S3 Vectors]
    RT --> BR[Bedrock Nova Lite<br/>guardrail e prompt versionado]
  end

  subgraph Site
    CF[CloudFront] --> SITE[(S3 do site)]
    SCH3[Agendamento<br/>minuto 30] --> PUB[Lambda de publicação] --> SITE
    DDB --> PUB
    CF -->|/api/chat com token| CHAT[Lambda do chat] --> RT
    COG[Cognito] -.->|login| CF
  end

  subgraph Operação
    Q[Lambda de qualidade<br/>minuto 40] --> CW[CloudWatch<br/>métricas e painel]
    DR[Lambda de drift<br/>diária] --> CW
    DDB --> Q
    CUR --> Q
    CUR --> DR
    CW --> SNS[Alertas por e-mail]
    CW -->|modelo pior por 7 dias| PIPE
    CT[CloudTrail]
    BUD[Orçamento com bloqueio]
  end
```

## Fluxo de uma previsão

1. **Minuto 5.** A coleta grava a nova rodada da Open-Meteo, o METAR das últimas horas e, quando
   houver acesso, a observação horária das estações do INMET. Cada objeto tem chave
   determinística, então uma nova tentativa só sobrescreve o anterior.
2. **Minuto 20.** A Lambda de previsão lê no SSM qual modelo está ativo e qual conjunto de
   variáveis ele usa. Depois monta as variáveis da hora, chama o endpoint e grava uma linha por
   horizonte no DynamoDB.
3. **Minuto 30.** A publicação gera o `forecasts.json` estático do site.
4. **Minuto 40.** A qualidade compara as previsões já vencidas com a chuva medida.
5. **A qualquer momento.** O assistente consulta a API, assinada com SigV4, e responde em hora
   de Brasília. No site, o chat só funciona com o token do Cognito.

## Modos de operação

O modelo ativo define de onde vem o estado do tempo da hora:

| Modo | Fonte do estado atual | Pontos |
|---|---|---|
| Completo | Observação horária das estações do INMET | Quatro |
| METAR | Boletim do aeroporto da Pampulha | Pampulha |

Trocar de modo é aprovar outra versão no registro de modelos. A Lambda de deploy atualiza o
endpoint e o parâmetro do modelo ativo, e a previsão da hora seguinte já usa o modo novo.

## Decisões

As decisões estão em [docs/adr](adr/README.md). Estas são as que mais definem o desenho:

- endpoint serverless (ADR-001);
- S3 Vectors como base vetorial (ADR-002);
- chuva observada nas estações do INMET como verdade (ADR-006);
- vazamento parcial nas variáveis de previsão histórica (ADR-009);
- deploy do modelo na aprovação (ADR-010);
- modo METAR (ADR-013).
