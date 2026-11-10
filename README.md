# chuva-bh

Rain nowcasting for Belo Horizonte on AWS: an MLOps platform and a GenAI agent that
measure their own quality against observed rainfall and the operational forecast.

## Status

Phase 0 (foundation) and Phase 1 (data) are deployed to `us-east-2`. Hourly ingestion is running
and the history is queryable in Athena. Phases 2 (model) and 3 (serving) are implemented and
tested as code, and their AWS resources have not been created.

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
