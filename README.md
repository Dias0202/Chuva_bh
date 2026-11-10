# chuva-bh

Rain nowcasting for Belo Horizonte on AWS: an MLOps platform and a GenAI agent that
measure their own quality against observed rainfall and the operational forecast.

**Result:** on the 2025 test year, the model's Brier score is 11.8% better than the best
baseline (calibrated persistence) for rain of at least 0.5 mm in the next 1 to 6 hours, and
PR-AUC goes from 0.16 to 0.37. Without numerical forecast features, which partly leak the
future in the historical data, the gain is 2.8%. See the [model card](docs/model-card.md).

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | KMS, buckets, CloudTrail, budget and kill switch | Deployed |
| 1 | Hourly ingestion, Parquet lake, Glue and Athena, 7 years of history | Deployed |
| 2 | Features, baselines, SageMaker pipeline, tuning, Clarify | Code and tests; trained locally |
| 3 | Serverless endpoint, hourly scoring, DynamoDB, HTTP API | Code and tests |
| 4 | LangGraph agent on AgentCore, Knowledge Base, Guardrail, evaluation | Code and tests |
| 5 | Quality and drift monitoring, dashboard, alarms, retraining | Code and tests |

Live scoring and monitoring need the hourly observation from the INMET API, whose access token
has been requested. Until then, observed rainfall arrives about one month late.

Architecture and request flow: [docs/architecture.md](docs/architecture.md). Decisions:
[docs/adr](docs/adr/README.md). Exam mapping: [docs/exam-mapping.md](docs/exam-mapping.md).

## Cost

Measured spend so far is in [docs/cost-log.md](docs/cost-log.md); billing data lags by up to a
day. The plan targets at most US$ 50 for the whole project. With every phase deployed, the
largest recurring items are expected to be CloudWatch custom metrics (about US$ 5 per month),
the KMS key (US$ 1 per month) and Bedrock tokens, which depend on use.

After the project, the suggestion is to keep only Phases 0, 1 and 3 running and remove the
rest with `scripts/teardown.py --phase N --yes`.

## Limitations

- One test year and four stations; summer storms are local, so a station may not represent
  nearby neighbourhoods.
- Forecast features from the historical API include runs issued after the prediction time.
  New runs are stored with their issue time, so retraining on them removes this bias.
- The agent's Guardrail topics are defined in English and still need to be measured on
  Portuguese questions.

## Requirements

Python 3.12 managed by [uv](https://docs.astral.sh/uv/), Node.js 24, and AWS CLI v2 for
deployment.

```bash
uv sync
npm ci
uv run pre-commit install
```

## Configuration

Deployment settings are read from the environment and are never stored in Git. See
[.env.example](.env.example).

| Variable | Required | Purpose |
|---|---|---|
| `CHUVA_BUDGET_EMAIL` | yes | Receives budget alerts and kill switch notifications |
| `CHUVA_KILL_SWITCH_USERS` | no | Comma-separated IAM users that also receive the deny policy |

## Checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
npx cdk synth --quiet
```

## Phase 0: foundation

`FoundationStack` provisions:

- One customer managed KMS key used by every bucket and by the audit trail.
- `raw`, `curated`, `artifacts` and `kb-docs` buckets, plus an `audit` bucket for CloudTrail.
  All block public access, use SSE-KMS with S3 Bucket Keys and reject non-TLS requests.
  Objects in `raw` expire after 30 days and objects in `audit` after 90 days.
- A multi-region CloudTrail trail for management events.
- A US$ 60 AWS Budget with email alerts at 25, 50, 80 and 100% of actual spend.
- A kill switch: at 90% an AWS Budgets action attaches a deny policy that blocks creating
  SageMaker, Bedrock and AgentCore resources.

Design notes:

- The budget excludes credits and refunds. With the AWS default the net cost of an account
  paid with credits stays at zero, so no alert would ever fire.
- The budget accumulates from 2026-11-01 instead of resetting monthly, because the limit
  applies to the whole project.
- The kill switch targets the CDK CloudFormation execution role, which is the identity that
  actually creates resources. It stops new resources only: running workloads keep running,
  and billing data can lag by several hours.
- Deployments run from a workstation for now, so there is no GitHub Actions deploy role.

### Deployment

```bash
npx cdk bootstrap aws://<account>/us-east-2 --tags Project=chuva-bh --tags Phase=0 --tags Owner=dias
npx cdk diff FoundationStack
npx cdk deploy FoundationStack
```

To report cost by tag, activate `Project` and `Phase` as cost allocation tags once they show
up in the Billing console, which can take up to 24 hours after the first deployment:

```bash
aws ce update-cost-allocation-tags-status --cost-allocation-tags-status \
  TagKey=Project,Status=Active TagKey=Phase,Status=Active
```

### Operations

```bash
uv run python scripts/teardown.py --phase 0 --dry-run  # list what would be deleted
uv run python scripts/teardown.py --phase 0 --yes      # delete
uv run python scripts/teardown.py --all --yes          # delete every phase, newest first
uv run python scripts/cost_report.py                   # update docs/cost-log.md
```

Teardown stops the trail, detaches the kill switch policy, empties the buckets and deletes
the stack. It leaves the `CDKToolkit` bootstrap stack in place. Each cost report makes at
least two Cost Explorer requests, at US$ 0.01 each.

## Phase 1: data

`IngestionStack` runs a Lambda every hour that stores the Open-Meteo operational forecast as raw
JSON and partitioned Parquet, with a dead letter queue for failures. Glue tables use partition
projection, so there is no recurring crawler, and an Athena workgroup caps each query at 1 GB.

Observed rainfall comes from the yearly INMET station archives, which are published about one
month late. See [ADR-006](docs/adr/006-ground-truth.md) for the source evaluation and its limits.

```bash
uv run python scripts/backfill.py --dry-run                         # process without writing
uv run python scripts/backfill.py                                   # load the full history
uv run python scripts/backfill.py --dataset observations --start-year 2026  # monthly refresh
```

Example queries are in [docs/athena-queries.md](docs/athena-queries.md), and the latest data
quality results are in [docs/data-quality.md](docs/data-quality.md).

## Phase 2: model

Features are pure pandas functions in `src/features`, shared by training and scoring. The model is
XGBoost, compared with two baselines calibrated on the training years: persistence and the
Open-Meteo forecast. The split is temporal: train through 2023, validate on 2024, test on 2025.

On the 2025 test set, a local run of `scripts/train_local.py` gave a Brier score 11.8% better than
the best baseline, or 2.8% better without forecast features. Forecast features from the
historical API leak part of the future, so the first number is optimistic
([ADR-009](docs/adr/009-vazamento-na-previsao-historica.md)). Details are in
[docs/evaluation.md](docs/evaluation.md).

`MlStack` holds the SageMaker execution role and the Model Registry group. The pipeline runs
Processing, Spot training, evaluation against the baselines, a condition on the Brier score and
registration pending manual approval. It is created by a script, not by CloudFormation:

```bash
uv run python scripts/train_local.py                    # local experiment, reads S3 only
uv run python scripts/sagemaker_jobs.py upsert          # create or update the pipeline
uv run python scripts/sagemaker_jobs.py start           # run it
uv run python scripts/sagemaker_jobs.py tune --train S3_URI --validation S3_URI
uv run python scripts/sagemaker_jobs.py clarify --model-package-arn ARN --test S3_URI
```

## Phase 3: serving

Approving a model in the registry triggers a Lambda that creates or updates the `chuva-bh-rain`
Serverless Endpoint and records the active version in SSM. Every hour, a scoring Lambda builds
the features for the current hour, calls the endpoint and writes one item per horizon to
DynamoDB, with a 90-day TTL. An HTTP API with IAM authentication and throttling serves
`GET /points` and `GET /forecast?point=<id>`.

Scoring needs the observation of the current hour, which only the INMET API provides. Until the
API token is configured, scoring records every point as skipped. See
[ADR-010](docs/adr/010-serving.md).

## Phase 4: agent

A LangGraph agent answers questions such as "vai chover na Pampulha às 18h?". It has three
read-only tools with schema-validated arguments: the forecast API, the list of points and a
Bedrock Knowledge Base over the project's own documents, stored in S3 Vectors. A Bedrock
Guardrail blocks off-topic requests and masks personal data, the system prompt is versioned in
Prompt Management, and the code runs on the AgentCore Runtime as a zip, without a container.

```bash
uv run python scripts/genai_ops.py sync-kb          # upload docs/kb and reindex
uv run python scripts/genai_ops.py enable-logging   # Bedrock invocation logging, 14 days
uv run python scripts/genai_ops.py ask "Vai chover na Pampulha às 18h?"
uv run python eval/run_eval.py                      # golden set, with LLM-as-a-judge
```

The golden set has 36 cases. The targets are 85% tool accuracy and 0.8 faithfulness. Deployment
risks, including the account SCP and cross-region inference, are in
[ADR-011](docs/adr/011-agente.md).

## Phase 5: operations

Every hour, a quality Lambda joins issued forecasts with observed rainfall and publishes the
Brier score of the model and of a calibrated persistence baseline over 7 and 30 days. Every day,
a drift Lambda computes the PSI of each observed feature against the training period. A
CloudWatch dashboard shows ingestion, quality, drift, API latency and agent tokens. Alarms email
the alert address, and the quality alarm starts the training pipeline, with manual approval
kept. See [ADR-012](docs/adr/012-operacao.md).

```bash
uv run python scripts/model_monitor_exercise.py export-baseline
uv run python scripts/model_monitor_exercise.py create --model-package-arn ARN --baseline-csv S3_URI
uv run python scripts/model_monitor_exercise.py traffic --endpoint NAME
uv run python scripts/model_monitor_exercise.py cleanup --endpoint NAME
uv run python scripts/ttl_sweeper.py --yes        # deletes anything whose TTL tag has expired
```

Audit queries for model approvals and deployments are in
[docs/cloudtrail-audit.md](docs/cloudtrail-audit.md).
