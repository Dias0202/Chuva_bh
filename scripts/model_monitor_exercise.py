#!/usr/bin/env python
"""Exercício do Model Monitor (ADR-003), num endpoint real-time temporário.

O endpoint é cobrado por hora: rode tudo no mesmo dia e termine com `cleanup`.
Todos os recursos recebem a tag TTL com a data de hoje, para o ttl_sweeper.

Uso:
    model_monitor_exercise.py export-baseline          CSV de referência a partir do cache local
    model_monitor_exercise.py create --model-package-arn ARN --baseline-csv S3_URI
    model_monitor_exercise.py traffic --endpoint NOME --requests 200
    model_monitor_exercise.py cleanup --endpoint NOME
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import boto3
import numpy as np
from botocore.exceptions import BotoCoreError, ClientError

ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from teardown import delete_sagemaker_resources  # noqa: E402

from features.build import FEATURE_SETS  # noqa: E402
from training.config import DEFAULT_TEST_YEAR  # noqa: E402

REGION: Final = "us-east-2"
PROJECT: Final = "chuva-bh"
INSTANCE_TYPE: Final = "ml.m5.large"
FEATURE_SET: Final = "full"
BASELINE_ROWS: Final = 5000
# Deslocamentos que simulam drift em features observadas.
DRIFT_SHIFTS: Final = {"relative_humidity_pct": 35.0, "pressure_tendency_3h_hpa": -6.0}


def _tags() -> list[dict[str, str]]:
    return [
        {"Key": "Project", "Value": PROJECT},
        {"Key": "Phase", "Value": "5"},
        {"Key": "Owner", "Value": "dias"},
        {"Key": "TTL", "Value": datetime.now(UTC).date().isoformat()},
    ]


def _outputs(session: boto3.Session, stack: str) -> dict[str, str]:
    description = session.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in description.get("Outputs", [])}


def export_baseline(session: boto3.Session) -> str:
    """Amostra do conjunto de teste, só features e com cabeçalho, como o Model Monitor espera."""
    from train_local import CACHE_DIR

    from training.dataset import read_partitioned
    from training.preprocess import prepare

    observations = read_partitioned(CACHE_DIR / "observations")
    forecasts = read_partitioned(CACHE_DIR / "forecasts")
    _, split = prepare(observations, forecasts, DEFAULT_TEST_YEAR)
    sample = split.test[list(FEATURE_SETS[FEATURE_SET])].sample(
        n=min(BASELINE_ROWS, len(split.test)), random_state=42
    )
    bucket = _outputs(session, f"{PROJECT}-foundation")["ArtifactsBucketName"]
    key = "sagemaker/monitor/baseline/baseline.csv"
    buffer = io.StringIO()
    sample.to_csv(buffer, index=False)
    session.client("s3").put_object(Bucket=bucket, Key=key, Body=buffer.getvalue().encode())
    return f"s3://{bucket}/{key}"


def create(session: boto3.Session, package_arn: str, baseline_csv: str) -> str:
    from sagemaker.model_monitor.cron_expression_generator import CronExpressionGenerator
    from sagemaker.model_monitor.dataset_format import DatasetFormat
    from sagemaker.model_monitor.model_monitoring import DefaultModelMonitor
    from sagemaker.session import Session

    ml = _outputs(session, f"{PROJECT}-ml")
    bucket = _outputs(session, f"{PROJECT}-foundation")["ArtifactsBucketName"]
    prefix = f"s3://{bucket}/sagemaker/monitor"
    name = f"{PROJECT}-monitor-{int(time.time())}"
    client = session.client("sagemaker")
    client.create_model(
        ModelName=name,
        PrimaryContainer={"ModelPackageName": package_arn},
        ExecutionRoleArn=ml["SageMakerRoleArn"],
        Tags=_tags(),  # type: ignore[arg-type]
    )
    client.create_endpoint_config(
        EndpointConfigName=name,
        ProductionVariants=[
            {
                "VariantName": "AllTraffic",
                "ModelName": name,
                "InstanceType": INSTANCE_TYPE,
                "InitialInstanceCount": 1,
            }
        ],
        DataCaptureConfig={
            "EnableCapture": True,
            "InitialSamplingPercentage": 100,
            "DestinationS3Uri": f"{prefix}/capture",
            "CaptureOptions": [{"CaptureMode": "Input"}, {"CaptureMode": "Output"}],
        },
        Tags=_tags(),  # type: ignore[arg-type]
    )
    client.create_endpoint(EndpointName=name, EndpointConfigName=name, Tags=_tags())  # type: ignore[arg-type]
    client.get_waiter("endpoint_in_service").wait(EndpointName=name)

    monitor = DefaultModelMonitor(
        role=ml["SageMakerRoleArn"],
        instance_count=1,
        instance_type=INSTANCE_TYPE,
        max_runtime_in_seconds=3600,
        sagemaker_session=Session(boto_session=session, default_bucket=bucket),
        tags=_tags(),
    )
    monitor.suggest_baseline(
        baseline_dataset=baseline_csv,
        dataset_format=DatasetFormat.csv(header=True),
        output_s3_uri=f"{prefix}/baseline/{name}",
        wait=True,
    )
    monitor.create_monitoring_schedule(
        monitor_schedule_name=name,
        endpoint_input=name,
        output_s3_uri=f"{prefix}/reports/{name}",
        statistics=monitor.baseline_statistics(),
        constraints=monitor.suggested_constraints(),
        schedule_cron_expression=CronExpressionGenerator.hourly(),
    )
    return name


def traffic(session: boto3.Session, endpoint: str, requests: int) -> None:
    """Envia linhas sintéticas com drift nas features escolhidas."""
    features = FEATURE_SETS[FEATURE_SET]
    rng = np.random.default_rng(7)
    runtime = session.client("sagemaker-runtime")
    for _ in range(requests):
        row = rng.normal(0.5, 0.2, len(features))
        row[0] = rng.choice([1, 3, 6])
        for feature, shift in DRIFT_SHIFTS.items():
            row[features.index(feature)] += shift
        runtime.invoke_endpoint(
            EndpointName=endpoint,
            ContentType="text/csv",
            Body=",".join(f"{v:.4f}" for v in row).encode(),
        )
    print(f"{requests} requisições enviadas a {endpoint}")


def cleanup(session: boto3.Session, endpoint: str) -> None:
    client = session.client("sagemaker")
    try:
        client.delete_monitoring_schedule(MonitoringScheduleName=endpoint)
    except ClientError as error:
        if "Could not find" not in error.response.get("Error", {}).get("Message", ""):
            raise
    delete_sagemaker_resources(
        client, [("endpoint", endpoint), ("endpoint-config", endpoint), ("model", endpoint)]
    )
    print(f"Recursos de {endpoint} apagados")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("export-baseline")
    p = sub.add_parser("create")
    p.add_argument("--model-package-arn", required=True)
    p.add_argument("--baseline-csv", required=True)
    p = sub.add_parser("traffic")
    p.add_argument("--endpoint", required=True)
    p.add_argument("--requests", type=int, default=200)
    p = sub.add_parser("cleanup")
    p.add_argument("--endpoint", required=True)
    args = parser.parse_args(argv)

    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    actions: dict[str, Any] = {
        "export-baseline": lambda: print(export_baseline(session)),
        "create": lambda: print(create(session, args.model_package_arn, args.baseline_csv)),
        "traffic": lambda: traffic(session, args.endpoint, args.requests),
        "cleanup": lambda: cleanup(session, args.endpoint),
    }
    try:
        actions[args.command]()
    except (ClientError, BotoCoreError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
