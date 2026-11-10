"""SageMaker Pipeline: Processing, Training Spot, Evaluation, Condition e Register."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sagemaker import image_uris
from sagemaker.estimator import Estimator
from sagemaker.inputs import TrainingInput
from sagemaker.model_metrics import MetricsSource, ModelMetrics
from sagemaker.processing import FrameworkProcessor, ProcessingInput, ProcessingOutput
from sagemaker.sklearn.estimator import SKLearn
from sagemaker.workflow.condition_step import ConditionStep
from sagemaker.workflow.conditions import ConditionLessThan
from sagemaker.workflow.entities import PipelineVariable
from sagemaker.workflow.fail_step import FailStep
from sagemaker.workflow.functions import Join, JsonGet
from sagemaker.workflow.parameters import ParameterInteger, ParameterString
from sagemaker.workflow.pipeline import Pipeline
from sagemaker.workflow.pipeline_context import PipelineSession
from sagemaker.workflow.properties import PropertyFile
from sagemaker.workflow.step_collections import RegisterModel
from sagemaker.workflow.steps import ProcessingStep, TrainingStep
from sagemaker.xgboost.estimator import XGBoost

from training.config import DEFAULT_FEATURE_SET, DEFAULT_TEST_YEAR, HYPERPARAMETERS
from training.names import MODEL_PACKAGE_GROUP, PIPELINE_NAME

XGBOOST_VERSION: Final = "1.7-1"
SKLEARN_VERSION: Final = "1.2-1"
MAX_RUNTIME_SECONDS: Final = 3600
SOURCE_DIR: Final = Path(__file__).resolve().parents[1]
TAGS: Final[list[dict[str, str | PipelineVariable]]] = [
    {"Key": "Project", "Value": "chuva-bh"},
    {"Key": "Phase", "Value": "2"},
    {"Key": "Owner", "Value": "dias"},
]
_PROCESSING: Final = "/opt/ml/processing"


@dataclass(frozen=True)
class PipelineSettings:
    role_arn: str
    artifacts_bucket: str
    curated_bucket: str
    kms_key_arn: str
    region: str


def _uri(step: ProcessingStep, output: str) -> Any:
    return step.properties.ProcessingOutputConfig.Outputs[output].S3Output.S3Uri


def build_pipeline(settings: PipelineSettings, session: PipelineSession) -> Pipeline:
    instance_type = ParameterString("InstanceType", default_value="ml.m5.large")
    test_year = ParameterInteger("TestYear", default_value=DEFAULT_TEST_YEAR)
    feature_set = ParameterString("FeatureSet", default_value=DEFAULT_FEATURE_SET)
    approval = ParameterString("ModelApprovalStatus", default_value="PendingManualApproval")
    prefix = f"s3://{settings.artifacts_bucket}/sagemaker"
    common: dict[str, Any] = {
        "role": settings.role_arn,
        "instance_count": 1,
        "instance_type": instance_type,
        "sagemaker_session": session,
        "max_runtime_in_seconds": MAX_RUNTIME_SECONDS,
        "output_kms_key": settings.kms_key_arn,
        "tags": TAGS,
    }

    preprocessor = FrameworkProcessor(
        estimator_cls=SKLearn,
        framework_version=SKLEARN_VERSION,
        base_job_name="chuva-bh-preprocess",
        **common,
    )
    preprocess = ProcessingStep(
        name="Preprocess",
        step_args=preprocessor.run(
            code="training/preprocess.py",
            source_dir=str(SOURCE_DIR),
            inputs=[
                ProcessingInput(
                    source=f"s3://{settings.curated_bucket}/{name}/",
                    destination=f"{_PROCESSING}/input/{name}",
                )
                for name in ("observations", "forecasts")
            ],
            outputs=[
                ProcessingOutput(output_name=name, source=f"{_PROCESSING}/output/{name}")
                for name in ("train", "validation", "test", "meta")
            ],
            arguments=[
                "--test-year",
                test_year.to_string(),  # type: ignore[no-untyped-call]
                "--feature-set",
                feature_set,
            ],
        ),
    )

    estimator = Estimator(
        image_uri=image_uris.retrieve("xgboost", settings.region, version=XGBOOST_VERSION),
        role=settings.role_arn,
        instance_count=1,
        instance_type=instance_type,
        use_spot_instances=True,
        max_run=MAX_RUNTIME_SECONDS,
        max_wait=MAX_RUNTIME_SECONDS,
        checkpoint_s3_uri=f"{prefix}/checkpoints",
        output_path=f"{prefix}/models",
        output_kms_key=settings.kms_key_arn,
        base_job_name="chuva-bh-train",
        sagemaker_session=session,
        hyperparameters={k: str(v) for k, v in HYPERPARAMETERS.items()},
        tags=TAGS,
    )
    parquet = "application/x-parquet"
    train = TrainingStep(
        name="Train",
        step_args=estimator.fit(
            {
                "train": TrainingInput(_uri(preprocess, "train"), content_type=parquet),
                "validation": TrainingInput(_uri(preprocess, "validation"), content_type=parquet),
            }
        ),
    )

    evaluator = FrameworkProcessor(
        estimator_cls=XGBoost,
        framework_version=XGBOOST_VERSION,
        base_job_name="chuva-bh-evaluate",
        **common,
    )
    report = PropertyFile(name="EvaluationReport", output_name="evaluation", path="evaluation.json")
    evaluate = ProcessingStep(
        name="Evaluate",
        step_args=evaluator.run(
            code="training/evaluate.py",
            source_dir=str(SOURCE_DIR),
            inputs=[
                ProcessingInput(
                    source=train.properties.ModelArtifacts.S3ModelArtifacts,
                    destination=f"{_PROCESSING}/model",
                ),
                *(
                    ProcessingInput(
                        source=_uri(preprocess, name), destination=f"{_PROCESSING}/{name}"
                    )
                    for name in ("validation", "test", "meta")
                ),
            ],
            outputs=[
                ProcessingOutput(output_name="evaluation", source=f"{_PROCESSING}/evaluation")
            ],
        ),
        property_files=[report],
    )

    register = RegisterModel(
        name="Register",
        estimator=estimator,
        model_data=train.properties.ModelArtifacts.S3ModelArtifacts,
        content_types=["text/csv"],
        response_types=["text/csv"],
        inference_instances=["ml.m5.large"],
        transform_instances=["ml.m5.large"],
        model_package_group_name=MODEL_PACKAGE_GROUP,
        approval_status=approval,
        customer_metadata_properties={"feature_set": feature_set},
        model_metrics=ModelMetrics(
            model_statistics=MetricsSource(
                s3_uri=Join(on="/", values=[_uri(evaluate, "evaluation"), "evaluation.json"]),
                content_type="application/json",
            )
        ),
    )
    beats_baseline = ConditionStep(
        name="BeatsBaseline",
        conditions=[
            # JsonGet é aceito pelo SDK em tempo de execução, mas falta nas anotações dele.
            ConditionLessThan(
                left=JsonGet(  # type: ignore[arg-type]
                    step_name=evaluate.name, property_file=report, json_path="metrics.model.brier"
                ),
                right=JsonGet(  # type: ignore[arg-type]
                    step_name=evaluate.name, property_file=report, json_path="best_baseline.brier"
                ),
            )
        ],
        if_steps=[register],
        else_steps=[
            FailStep(
                name="BelowBaseline",
                error_message="O Brier do modelo não superou a melhor baseline",
            )
        ],
    )

    return Pipeline(
        name=PIPELINE_NAME,
        parameters=[instance_type, test_year, feature_set, approval],
        steps=[preprocess, train, evaluate, beats_baseline],
        sagemaker_session=session,
    )
