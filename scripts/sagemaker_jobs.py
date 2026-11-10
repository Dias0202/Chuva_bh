#!/usr/bin/env python
"""Operações do SageMaker que não ficam no CDK. Todas criam recursos pagos.

Uso:
    sagemaker_jobs.py upsert                       cria ou atualiza o pipeline
    sagemaker_jobs.py start                        inicia uma execução do pipeline
    sagemaker_jobs.py tune --train S3 --validation S3
    sagemaker_jobs.py clarify --model-package-arn ARN --test S3
    sagemaker_jobs.py isolated-train --train S3 --validation S3

Os URIs de entrada são as saídas do passo Preprocess de uma execução anterior.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import boto3
from botocore.exceptions import BotoCoreError, ClientError

ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from features.build import FEATURE_SETS, LABEL  # noqa: E402
from training.config import DEFAULT_FEATURE_SET, HYPERPARAMETERS  # noqa: E402

REGION: Final = "us-east-2"
PROJECT: Final = "chuva-bh"
MAX_TUNING_JOBS: Final = 10
MAX_PARALLEL_JOBS: Final = 2
MAX_RUNTIME_SECONDS: Final = 3600


def stack_outputs(session: boto3.Session, stack_name: str) -> dict[str, str]:
    stack = session.client("cloudformation").describe_stacks(StackName=stack_name)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def _context(profile: str | None) -> dict[str, Any]:
    from sagemaker.workflow.pipeline_context import PipelineSession

    session = boto3.Session(profile_name=profile, region_name=REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    foundation = stack_outputs(session, f"{PROJECT}-foundation")
    ml = stack_outputs(session, f"{PROJECT}-ml")
    artifacts = foundation["ArtifactsBucketName"]
    return {
        "boto": session,
        "account": account,
        "role": ml["SageMakerRoleArn"],
        "kms": foundation["KmsKeyArn"],
        "curated": foundation["CuratedBucketName"],
        "artifacts": artifacts,
        "ml": ml,
        "session": PipelineSession(
            boto_session=session, default_bucket=artifacts, default_bucket_prefix="sagemaker"
        ),
    }


def _estimator(ctx: dict[str, Any], **extra: Any) -> Any:
    from sagemaker import image_uris
    from sagemaker.estimator import Estimator
    from sagemaker.session import Session

    from training.pipeline import TAGS, XGBOOST_VERSION

    prefix = f"s3://{ctx['artifacts']}/sagemaker"
    return Estimator(
        image_uri=image_uris.retrieve("xgboost", REGION, version=XGBOOST_VERSION),
        role=ctx["role"],
        instance_count=1,
        instance_type="ml.m5.large",
        use_spot_instances=True,
        max_run=MAX_RUNTIME_SECONDS,
        max_wait=MAX_RUNTIME_SECONDS,
        output_path=f"{prefix}/models",
        output_kms_key=ctx["kms"],
        sagemaker_session=Session(boto_session=ctx["boto"], default_bucket=ctx["artifacts"]),
        hyperparameters={k: str(v) for k, v in HYPERPARAMETERS.items()},
        tags=TAGS,
        **extra,
    )


def _channels(args: argparse.Namespace) -> dict[str, Any]:
    from sagemaker.inputs import TrainingInput

    parquet = "application/x-parquet"
    return {
        "train": TrainingInput(args.train, content_type=parquet),
        "validation": TrainingInput(args.validation, content_type=parquet),
    }


def upsert(ctx: dict[str, Any], start: bool) -> None:
    from training.pipeline import TAGS, PipelineSettings, build_pipeline

    settings = PipelineSettings(
        role_arn=ctx["role"],
        artifacts_bucket=ctx["artifacts"],
        curated_bucket=ctx["curated"],
        kms_key_arn=ctx["kms"],
        region=REGION,
    )
    pipeline = build_pipeline(settings, ctx["session"])
    pipeline.upsert(role_arn=ctx["role"], tags=TAGS)
    print(f"Pipeline {pipeline.name} atualizado")
    if start:
        execution = pipeline.start()
        print(f"Execução iniciada: {execution.arn}")


def tune(ctx: dict[str, Any], args: argparse.Namespace) -> None:
    from sagemaker.parameter import ContinuousParameter, IntegerParameter
    from sagemaker.tuner import HyperparameterTuner

    tuner = HyperparameterTuner(
        estimator=_estimator(ctx),
        objective_metric_name="validation:logloss",
        objective_type="Minimize",
        strategy="Bayesian",
        max_jobs=MAX_TUNING_JOBS,
        max_parallel_jobs=MAX_PARALLEL_JOBS,
        hyperparameter_ranges={
            "eta": ContinuousParameter(0.02, 0.3, scaling_type="Logarithmic"),
            "max_depth": IntegerParameter(3, 8),
            "min_child_weight": IntegerParameter(1, 20),
            "subsample": ContinuousParameter(0.6, 1.0),
        },
        base_tuning_job_name=f"{PROJECT}-tune",
    )
    tuner.fit(_channels(args), wait=False)
    job = tuner.latest_tuning_job
    print(f"Tuning iniciado: {job.name if job else 'nome indisponível'}")


def clarify(ctx: dict[str, Any], args: argparse.Namespace) -> None:
    """SHAP sobre o conjunto de teste. O Clarify cria e apaga um endpoint temporário."""
    from sagemaker import clarify as sm_clarify
    from sagemaker.session import Session

    sagemaker_client = ctx["boto"].client("sagemaker")
    model_name = f"{PROJECT}-clarify-{int(time.time())}"
    sagemaker_client.create_model(
        ModelName=model_name,
        PrimaryContainer={"ModelPackageName": args.model_package_arn},
        ExecutionRoleArn=ctx["role"],
    )
    features = list(FEATURE_SETS[args.feature_set])
    try:
        processor = sm_clarify.SageMakerClarifyProcessor(
            role=ctx["role"],
            instance_count=1,
            instance_type="ml.m5.large",
            max_runtime_in_seconds=MAX_RUNTIME_SECONDS,
            sagemaker_session=Session(boto_session=ctx["boto"], default_bucket=ctx["artifacts"]),
        )
        processor.run_explainability(
            data_config=sm_clarify.DataConfig(
                s3_data_input_path=args.test,
                s3_output_path=f"s3://{ctx['artifacts']}/sagemaker/clarify/{model_name}",
                label=LABEL,
                headers=[LABEL, *features],
                dataset_type="application/x-parquet",
            ),
            model_config=sm_clarify.ModelConfig(
                model_name=model_name,
                instance_type="ml.m5.large",
                instance_count=1,
                accept_type="text/csv",
                content_type="text/csv",
            ),
            explainability_config=sm_clarify.SHAPConfig(
                num_samples=100, agg_method="mean_abs", save_local_shap_values=False
            ),
        )
    finally:
        sagemaker_client.delete_model(ModelName=model_name)


def isolated_train(ctx: dict[str, Any], args: argparse.Namespace) -> None:
    ml = ctx["ml"]
    if "IsolatedSubnetIds" not in ml:
        raise SystemExit("Implante a MlStack com -c isolation=true antes deste exercício")
    estimator = _estimator(
        ctx,
        enable_network_isolation=True,
        subnets=ml["IsolatedSubnetIds"].split(","),
        security_group_ids=[ml["IsolatedSecurityGroupId"]],
        base_job_name=f"{PROJECT}-isolated",
    )
    estimator.fit(_channels(args), wait=False)
    print(f"Treino isolado iniciado: {estimator.latest_training_job.name}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("upsert")
    sub.add_parser("start")
    for name in ("tune", "isolated-train"):
        p = sub.add_parser(name)
        p.add_argument("--train", required=True)
        p.add_argument("--validation", required=True)
    p = sub.add_parser("clarify")
    p.add_argument("--model-package-arn", required=True)
    p.add_argument("--test", required=True)
    p.add_argument("--feature-set", choices=sorted(FEATURE_SETS), default=DEFAULT_FEATURE_SET)
    args = parser.parse_args(argv)

    try:
        ctx = _context(args.profile)
        if args.command in ("upsert", "start"):
            upsert(ctx, start=args.command == "start")
        elif args.command == "tune":
            tune(ctx, args)
        elif args.command == "clarify":
            clarify(ctx, args)
        else:
            isolated_train(ctx, args)
    except (ClientError, BotoCoreError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
