# chuva-bh

Rain nowcasting for Belo Horizonte on AWS: an MLOps platform and a GenAI agent that
measure their own quality against observed rainfall and the operational forecast.

## Status

Phase 0 (foundation) is implemented and synthesizes locally. It has not been deployed yet.

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
