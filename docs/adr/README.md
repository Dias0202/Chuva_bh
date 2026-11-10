# Decisões de arquitetura

As decisões 001 a 005 e 007 são curtas e ficam registradas aqui; as demais têm arquivo próprio.

| # | Decisão | Motivo | Alternativa descartada |
|---|---|---|---|
| 001 | Serverless Inference em vez de endpoint real-time | Cerca de 72 invocações por dia; o real-time cobra por hora mesmo ocioso | Endpoint ml.m5.large ligado 24 horas |
| 002 | S3 Vectors como vector store da Knowledge Base | O OpenSearch Serverless tem piso de 2 OCUs, perto de US$ 345 por mês sem tráfego | OpenSearch Serverless |
| 003 | Qualidade e drift por Lambdas próprias e CloudWatch | O Serverless Inference não tem data capture; o Model Monitor é exercitado num endpoint temporário | Endpoint real-time só para o Model Monitor |
| 004 | Sem VPC nem NAT nos componentes | O NAT cobra por hora e não agrega ao objetivo; o isolamento de rede é exercitado num treino com S3 gateway endpoint | NAT Gateway |
| 005 | Deploy da estação de trabalho com `aws login`; o GitHub Actions só valida | Sem chaves de longa duração e sem role de deploy para manter enquanto o deploy for manual | Role de deploy com OIDC; access keys em secrets |
| [006](006-ground-truth.md) | Ground truth nas estações automáticas do INMET | Reanálise e previsão não são observação | Arquivo histórico da Open-Meteo como verdade |
| 007 | Região us-east-2 | Uma SCP da organização nega o acesso a us-east-1 | us-east-1 |
| [008](008-lambda-sem-binarios.md) | Lambda de ingestão com a layer do AWS SDK for pandas | Escreve Parquet sem empacotar binários a partir do Windows | Build próprio de `pyarrow` |
| [009](009-vazamento-na-previsao-historica.md) | Treinar com e sem as features de previsão histórica | A série histórica não informa a rodada de origem | Usar a série sem ressalva |
| [010](010-serving.md) | Deploy do endpoint na aprovação, API com IAM, empacotamento com uv | Sem deploy de stack por modelo, sem API pública e sem Docker | Endpoint no CloudFormation |
| [011](011-agente.md) | Agente LangGraph no AgentCore Runtime, com código em zip | Ferramentas só de leitura e sem imagem de contêiner | Bedrock Agents; contêiner no ECR |
| [012](012-operacao.md) | Qualidade e drift com referência calculada dos dados e retreino por alarme | Independe dos artefatos de treino e mantém o custo baixo | Model Monitor permanente |
