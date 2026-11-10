"""Fase 1: ingestão horária, catálogo Glue e consultas Athena."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_athena as athena
from aws_cdk import aws_glue as glue
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from ingestion.columns import FORECASTS, OBSERVATIONS, Dataset
from ingestion.points import POINT_IDS
from stacks.common import PROJECT, REGION, apply_project_tags

PHASE: Final = "1"

SOURCE_DIR: Final = Path(__file__).resolve().parents[2] / "src"
# Layer oficial do AWS SDK for pandas: traz o pyarrow para Linux sem empacotar binários.
PANDAS_LAYER_ARN: Final = f"arn:aws:lambda:{REGION}:336392948345:layer:AWSSDKPandas-Python312:32"

FUNCTION_NAME: Final = f"{PROJECT}-ingest"
SCHEDULE_NAME: Final = f"{PROJECT}-ingest-hourly"
# Minuto 5: dá tempo de a rodada da hora ser publicada e evita o pico da virada da hora.
SCHEDULE_EXPRESSION: Final = "cron(5 * * * ? *)"
METRIC_NAMESPACE: Final = "ChuvaBh/Ingestion"
RETRY_ATTEMPTS: Final = 2

DATABASE_NAME: Final = "chuva_bh"
WORKGROUP_NAME: Final = PROJECT
ATHENA_BYTES_LIMIT: Final = 1024**3
FIRST_PARTITION_YEAR: Final = 2019
LAST_PARTITION_YEAR: Final = 2036

_PARQUET_INPUT: Final = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
_PARQUET_OUTPUT: Final = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"
_PARQUET_SERDE: Final = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"


class IngestionStack(Stack):
    """Coleta dados a cada hora e os expõe para consulta."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        raw_bucket: s3.IBucket,
        curated_bucket: s3.IBucket,
        artifacts_bucket: s3.IBucket,
        key: kms.IKey,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        queue = sqs.Queue(
            self,
            "IngestDeadLetterQueue",
            queue_name=f"{PROJECT}-ingest-dlq",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=Duration.days(14),
        )
        function = self._create_function(raw_bucket, curated_bucket, queue)
        self._create_schedule(function, queue)
        self._create_catalog(curated_bucket)
        self._create_workgroup(artifacts_bucket, key)

        apply_project_tags(self, PHASE)

    def _create_function(
        self, raw_bucket: s3.IBucket, curated_bucket: s3.IBucket, queue: sqs.IQueue
    ) -> lambda_.Function:
        log_group = logs.LogGroup(
            self,
            "IngestLogGroup",
            log_group_name=f"/aws/lambda/{FUNCTION_NAME}",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        function = lambda_.Function(
            self,
            "IngestFunction",
            function_name=FUNCTION_NAME,
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="ingestion.handler.handler",
            code=lambda_.Code.from_asset(
                str(SOURCE_DIR), exclude=["**/__pycache__", "**/__pycache__/**", "**/*.pyc"]
            ),
            layers=[
                lambda_.LayerVersion.from_layer_version_arn(self, "PandasLayer", PANDAS_LAYER_ARN)
            ],
            memory_size=512,
            timeout=Duration.seconds(120),
            log_group=log_group,
            retry_attempts=RETRY_ATTEMPTS,
            dead_letter_queue=queue,
            environment={
                "RAW_BUCKET": raw_bucket.bucket_name,
                "CURATED_BUCKET": curated_bucket.bucket_name,
            },
        )
        raw_bucket.grant_put(function, "open_meteo/*")
        curated_bucket.grant_put(function, f"{FORECASTS.name}/*")
        # PutMetricData não aceita ARN de recurso: o escopo é dado pela condição.
        function.add_to_role_policy(
            iam.PolicyStatement(
                sid="PublishIngestionMetrics",
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": METRIC_NAMESPACE}},
            )
        )
        return function

    def _create_schedule(self, function: lambda_.IFunction, queue: sqs.IQueue) -> None:
        role = iam.Role(
            self,
            "IngestSchedulerRole",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
        )
        function.grant_invoke(role)
        queue.grant_send_messages(role)

        scheduler.CfnSchedule(
            self,
            "IngestSchedule",
            name=SCHEDULE_NAME,
            schedule_expression=SCHEDULE_EXPRESSION,
            schedule_expression_timezone="UTC",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=function.function_arn,
                role_arn=role.role_arn,
                input=json.dumps({"scheduled_time": "<aws.scheduler.scheduled-time>"}),
                retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(
                    maximum_retry_attempts=RETRY_ATTEMPTS,
                    maximum_event_age_in_seconds=3600,
                ),
                dead_letter_config=scheduler.CfnSchedule.DeadLetterConfigProperty(
                    arn=queue.queue_arn
                ),
            ),
        )

    def _create_catalog(self, curated_bucket: s3.IBucket) -> None:
        database = glue.CfnDatabase(
            self,
            "Database",
            catalog_id=self.account,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name=DATABASE_NAME, description="Data lake do projeto chuva-bh"
            ),
        )
        for dataset in (OBSERVATIONS, FORECASTS):
            self._create_table(database, curated_bucket, dataset)

    def _create_table(
        self, database: glue.CfnDatabase, curated_bucket: s3.IBucket, dataset: Dataset
    ) -> None:
        location = f"s3://{curated_bucket.bucket_name}/{dataset.name}"
        # Partition projection: o Athena deduz as partições, sem crawler nem MSCK REPAIR.
        projection = {
            "projection.enabled": "true",
            "projection.year.type": "integer",
            "projection.year.range": f"{FIRST_PARTITION_YEAR},{LAST_PARTITION_YEAR}",
            "projection.month.type": "integer",
            "projection.month.range": "1,12",
            "projection.month.digits": "2",
            "projection.point_id.type": "enum",
            "projection.point_id.values": ",".join(POINT_IDS),
            "storage.location.template": (
                f"{location}/year=${{year}}/month=${{month}}/point_id=${{point_id}}/"
            ),
        }
        glue.CfnTable(
            self,
            f"{dataset.name.title()}Table",
            catalog_id=self.account,
            database_name=database.ref,
            table_input=glue.CfnTable.TableInputProperty(
                name=dataset.name,
                table_type="EXTERNAL_TABLE",
                parameters={"classification": "parquet", **projection},
                partition_keys=[
                    glue.CfnTable.ColumnProperty(name="year", type="int"),
                    glue.CfnTable.ColumnProperty(name="month", type="int"),
                    glue.CfnTable.ColumnProperty(name="point_id", type="string"),
                ],
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    columns=[
                        glue.CfnTable.ColumnProperty(name=column.name, type=column.glue_type)
                        for column in dataset.columns
                    ],
                    location=f"{location}/",
                    input_format=_PARQUET_INPUT,
                    output_format=_PARQUET_OUTPUT,
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library=_PARQUET_SERDE
                    ),
                ),
            ),
        )

    def _create_workgroup(self, artifacts_bucket: s3.IBucket, key: kms.IKey) -> None:
        athena.CfnWorkGroup(
            self,
            "WorkGroup",
            name=WORKGROUP_NAME,
            description="Consultas do projeto chuva-bh, com limite de dados por consulta",
            recursive_delete_option=True,
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                enforce_work_group_configuration=True,
                bytes_scanned_cutoff_per_query=ATHENA_BYTES_LIMIT,
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{artifacts_bucket.bucket_name}/athena-results/",
                    encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                        encryption_option="SSE_KMS", kms_key=key.key_arn
                    ),
                ),
            ),
        )
