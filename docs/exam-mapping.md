# Mapeamento para o AWS Certified Machine Learning Engineer – Associate (MLA-C02)

| Componente | Onde está | Domínio | O que exercita |
|---|---|---|---|
| Ingestão horária, Parquet particionado, Glue com partition projection, Athena com limite por consulta | `src/ingestion`, `infra/stacks/ingestion_stack.py` | D1 | Ingestão, formatos, catálogo, consulta e custo |
| Validação de faixas, duplicidade e lacunas, com métricas no CloudWatch | `src/ingestion/quality.py`, `docs/data-quality.md` | D1 | Qualidade e validação de dados |
| Avaliação das fontes de ground truth | `docs/adr/006-ground-truth.md` | D1 | Escolha de fonte, licença e latência dos dados |
| Features, split temporal e vazamento | `src/features`, `src/training/dataset.py`, ADR-009 | D1, D2 | Feature engineering, vazamento temporal, classes raras |
| Corpus, chunking e S3 Vectors | `docs/kb`, `infra/stacks/genai_stack.py` | D1 | Preparação de dados para GenAI e vector stores |
| XGBoost built-in, Spot com checkpoint, tuning Bayesiano | `src/training/pipeline.py`, `scripts/sagemaker_jobs.py` | D2 | Treino, HPO e custo de treino |
| Baselines calibradas, Brier, PR-AUC, CSI, confiabilidade | `src/training` | D2 | Avaliação de modelos probabilísticos |
| SHAP com TreeSHAP (o Clarify está em manutenção para contas novas) | `scripts/explain.py`, `docs/explainability.md` | D2 | Explicabilidade |
| Model Registry com aprovação manual | `infra/stacks/ml_stack.py` | D2, D3 | Versionamento e governança de modelos |
| Escolha de modelo, prompt versionado e avaliação com LLM-as-a-judge | `src/agent`, `eval/` | D2 | Seleção e avaliação de foundation models |
| SageMaker Pipelines com condição sobre o Brier | `src/training/pipeline.py` | D3 | Orquestração de ML |
| Serverless Inference implantado na aprovação | `src/serving/deployer.py`, ADR-010 | D3 | Opções de inferência e deploy automatizado |
| Scoring agendado, DynamoDB e HTTP API com IAM | `src/serving`, `infra/stacks/serving_stack.py` | D3 | Inferência em lote agendada e exposição por API |
| Agente LangGraph no AgentCore Runtime, Knowledge Base e Guardrails | `src/agent`, `infra/stacks/genai_stack.py` | D3 | Deploy de RAG e agentes |
| CDK, CI no GitHub Actions e empacotamento sem Docker | `infra/`, `.github/workflows/ci.yml` | D3 | IaC e CI/CD |
| Qualidade contra ground truth, PSI, dashboard e alarmes | `src/monitoring`, `infra/stacks/observability_stack.py` | D4 | Monitoramento de modelo e de infraestrutura |
| Model Monitor em endpoint temporário | `scripts/model_monitor_exercise.py` | D4 | Data capture, baseline e violações |
| Retreino disparado por alarme | `infra/stacks/observability_stack.py` | D4 | Retreino automatizado |
| IAM de menor privilégio, KMS, CloudTrail, kill switch e TTL | `infra/stacks/foundation_stack.py`, `scripts/ttl_sweeper.py` | D4 | Segurança, auditoria e controle de custo |

Fica só na teoria: fine-tuning e distillation no Bedrock, Custom Model Import, multi-model
endpoints, treino distribuído, Inferentia e Trainium.
