"""Fase 5: qualidade e drift, dashboard, alarmes por e-mail e gatilho de retreino."""

from __future__ import annotations

from typing import Any, Final

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from constructs import Construct

from agent.constants import DEFAULT_MODEL_ID
from stacks.common import PROJECT, apply_project_tags
from stacks.ingestion_stack import PANDAS_LAYER_ARN, SOURCE_DIR
from stacks.scheduling import RETRY_ATTEMPTS, dead_letter_queue, log_group, schedule
from training.names import PIPELINE_NAME

PHASE: Final = "5"
QUALITY_SCHEDULE: Final = "cron(40 * * * ? *)"
DRIFT_SCHEDULE: Final = "cron(0 6 * * ? *)"
PSI_THRESHOLD: Final = 0.25
# Preço de tabela do Nova Lite em us-east-2 (US$ por milhão de tokens), conferir antes de usar.
NOVA_LITE_INPUT_PRICE: Final = 0.06
NOVA_LITE_OUTPUT_PRICE: Final = 0.24
_EXCLUDE: Final = ["**/__pycache__", "**/__pycache__/**", "**/*.pyc", "agent", "agent_main.py"]


class ObservabilityStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        curated_bucket: s3.IBucket,
        artifacts_bucket: s3.IBucket,
        table: dynamodb.ITable,
        api: apigw.HttpApi,
        alert_email: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        code = lambda_.Code.from_asset(str(SOURCE_DIR), exclude=_EXCLUDE)
        layer = lambda_.LayerVersion.from_layer_version_arn(self, "PandasLayer", PANDAS_LAYER_ARN)
        quality = self._monitor_function(
            "Quality",
            "monitoring.quality.handler",
            code,
            layer,
            QUALITY_SCHEDULE,
            {"CURATED_BUCKET": curated_bucket.bucket_name, "TABLE_NAME": table.table_name},
            "ChuvaBh/Quality",
        )
        drift = self._monitor_function(
            "Drift",
            "monitoring.drift.handler",
            code,
            layer,
            DRIFT_SCHEDULE,
            {
                "CURATED_BUCKET": curated_bucket.bucket_name,
                "ARTIFACTS_BUCKET": artifacts_bucket.bucket_name,
            },
            "ChuvaBh/Drift",
        )
        curated_bucket.grant_read(quality, "observations/*")
        curated_bucket.grant_read(drift, "observations/*")
        table.grant(quality, "dynamodb:Query")
        artifacts_bucket.grant_read(drift, "monitoring/*")
        artifacts_bucket.grant_put(drift, "monitoring/*")

        topic = sns.Topic(self, "AlertsTopic", topic_name=f"{PROJECT}-alerts")
        topic.add_subscription(subscriptions.EmailSubscription(alert_email))
        alarms = self._create_alarms(api)
        for alarm in alarms.values():
            alarm.add_alarm_action(cw_actions.SnsAction(topic))
        self._create_retrain_trigger(alarms["ModelWorseThanBaseline"])
        self._create_dashboard(api)

        apply_project_tags(self, PHASE)
        CfnOutput(self, "AlertsTopicArn", value=topic.topic_arn)

    def _monitor_function(
        self,
        name: str,
        handler: str,
        code: lambda_.Code,
        layer: lambda_.ILayerVersion,
        expression: str,
        environment: dict[str, str],
        namespace: str,
    ) -> lambda_.Function:
        function_name = f"{PROJECT}-{name.lower()}"
        queue = dead_letter_queue(self, f"{name}DeadLetterQueue", f"{function_name}-dlq")
        function = lambda_.Function(
            self,
            f"{name}Function",
            function_name=function_name,
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler=handler,
            code=code,
            layers=[layer],
            memory_size=1536,
            timeout=Duration.minutes(5),
            log_group=log_group(self, f"{name}LogGroup", function_name),
            retry_attempts=RETRY_ATTEMPTS,
            dead_letter_queue=queue,
            environment=environment,
        )
        function.add_to_role_policy(
            iam.PolicyStatement(
                sid=f"Publish{name}Metrics",
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": namespace}},
            )
        )
        schedule(
            self,
            f"{name}Schedule",
            name=f"{function_name}-schedule",
            expression=expression,
            function=function,
            queue=queue,
        )
        return function

    def _create_alarms(self, api: apigw.HttpApi) -> dict[str, cw.Alarm]:
        ingestion = cw.Metric(
            namespace="ChuvaBh/Ingestion",
            metric_name="RowsWritten",
            dimensions_map={"Dataset": "forecasts"},
            statistic="Sum",
            period=Duration.hours(1),
        )
        quality = cw.Metric(
            namespace="ChuvaBh/Quality",
            metric_name="BrierDifference",
            dimensions_map={"Window": "7d"},
            statistic="Average",
            period=Duration.days(1),
        )
        drift = cw.Metric(
            namespace="ChuvaBh/Drift",
            metric_name="MaxPsi",
            statistic="Maximum",
            period=Duration.days(1),
        )
        errors = cw.Metric(
            namespace="AWS/ApiGateway",
            metric_name="5xx",
            dimensions_map={"ApiId": api.api_id},
            statistic="Sum",
            period=Duration.minutes(5),
        )
        specs = {
            "IngestionStopped": (
                ingestion,
                1,
                cw.ComparisonOperator.LESS_THAN_THRESHOLD,
                2,
                2,
                cw.TreatMissingData.BREACHING,
                "Nenhuma previsão ingerida por 2 horas seguidas",
            ),
            "ModelWorseThanBaseline": (
                quality,
                0,
                cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                7,
                7,
                cw.TreatMissingData.NOT_BREACHING,
                "Brier do modelo pior que a persistência por 7 dias seguidos",
            ),
            "DriftHigh": (
                drift,
                PSI_THRESHOLD,
                cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                1,
                1,
                cw.TreatMissingData.NOT_BREACHING,
                "PSI de alguma feature acima de 0,25",
            ),
            "ApiErrors": (
                errors,
                0,
                cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                3,
                2,
                cw.TreatMissingData.NOT_BREACHING,
                "Erros 5xx na API de previsões",
            ),
        }
        return {
            name: cw.Alarm(
                self,
                f"{name}Alarm",
                alarm_name=f"{PROJECT}-{name}",
                alarm_description=description,
                metric=metric,
                threshold=threshold,
                comparison_operator=operator,
                evaluation_periods=periods,
                datapoints_to_alarm=datapoints,
                treat_missing_data=missing,
            )
            for name, (
                metric,
                threshold,
                operator,
                periods,
                datapoints,
                missing,
                description,
            ) in specs.items()
        }

    def _create_retrain_trigger(self, alarm: cw.Alarm) -> None:
        """Alarme de qualidade inicia o pipeline. O registro continua pedindo aprovação manual."""
        pipeline_arn = (
            f"arn:{self.partition}:sagemaker:{self.region}:{self.account}:pipeline/{PIPELINE_NAME}"
        )
        role = iam.Role(
            self,
            "RetrainRole",
            assumed_by=iam.ServicePrincipal(
                "events.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="StartTrainingPipeline",
                actions=["sagemaker:StartPipelineExecution"],
                resources=[pipeline_arn],
            )
        )
        events.CfnRule(
            self,
            "RetrainRule",
            description="Alarme de qualidade do modelo dispara o retreino",
            event_pattern={
                "source": ["aws.cloudwatch"],
                "detail-type": ["CloudWatch Alarm State Change"],
                "resources": [alarm.alarm_arn],
                "detail": {"state": {"value": ["ALARM"]}},
            },
            targets=[
                events.CfnRule.TargetProperty(
                    id="RetrainPipeline",
                    arn=pipeline_arn,
                    role_arn=role.role_arn,
                    sage_maker_pipeline_parameters=events.CfnRule.SageMakerPipelineParametersProperty(
                        pipeline_parameter_list=[
                            events.CfnRule.SageMakerPipelineParameterProperty(
                                name="ModelApprovalStatus", value="PendingManualApproval"
                            )
                        ]
                    ),
                    retry_policy=events.CfnRule.RetryPolicyProperty(
                        maximum_retry_attempts=RETRY_ATTEMPTS, maximum_event_age_in_seconds=3600
                    ),
                )
            ],
        )

    def _create_dashboard(self, api: apigw.HttpApi) -> None:
        def metric(namespace: str, name: str, statistic: str = "Sum", **dims: str) -> cw.Metric:
            return cw.Metric(
                namespace=namespace,
                metric_name=name,
                dimensions_map=dims or None,
                statistic=statistic,
                period=Duration.hours(1),
            )

        tokens_in = metric("AWS/Bedrock", "InputTokenCount", ModelId=DEFAULT_MODEL_ID)
        tokens_out = metric("AWS/Bedrock", "OutputTokenCount", ModelId=DEFAULT_MODEL_ID)
        cost = cw.MathExpression(
            expression=(
                f"(tin * {NOVA_LITE_INPUT_PRICE} + tout * {NOVA_LITE_OUTPUT_PRICE}) / 1000000"
            ),
            using_metrics={"tin": tokens_in, "tout": tokens_out},
            label="Custo estimado do agente (US$)",
            period=Duration.hours(1),
        )
        width = 12
        widgets: list[list[cw.IWidget]] = [
            [
                cw.TextWidget(
                    markdown="# chuva-bh\nIngestão, qualidade contra a chuva observada, drift, "
                    "API e agente.",
                    width=24,
                    height=2,
                )
            ],
            [
                cw.GraphWidget(
                    title="Ingestão: linhas por hora",
                    left=[
                        metric("ChuvaBh/Ingestion", "RowsWritten", Dataset="forecasts"),
                        metric("ChuvaBh/Ingestion", "RowsWritten", Dataset="observations"),
                    ],
                    right=[metric("ChuvaBh/Ingestion", "PointsFailed", Dataset="forecasts")],
                    width=width,
                ),
                cw.GraphWidget(
                    title="Brier: modelo contra persistência",
                    left=[
                        metric("ChuvaBh/Quality", "BrierModel", "Average", Window="7d"),
                        metric("ChuvaBh/Quality", "BrierBaseline", "Average", Window="7d"),
                        metric("ChuvaBh/Quality", "BrierModel", "Average", Window="30d"),
                        metric("ChuvaBh/Quality", "BrierBaseline", "Average", Window="30d"),
                    ],
                    width=width,
                ),
            ],
            [
                cw.GraphWidget(
                    title="Drift (PSI)",
                    left=[metric("ChuvaBh/Drift", "MaxPsi", "Maximum")],
                    left_annotations=[cw.HorizontalAnnotation(value=PSI_THRESHOLD, label="limite")],
                    width=width,
                ),
                cw.GraphWidget(
                    title="Scoring por hora",
                    left=[
                        metric("ChuvaBh/Serving", "PointsScored"),
                        metric("ChuvaBh/Serving", "PointsSkipped"),
                        metric("ChuvaBh/Serving", "PointsFailed"),
                    ],
                    width=width,
                ),
            ],
            [
                cw.GraphWidget(
                    title="API: latência p95 e erros",
                    left=[metric("AWS/ApiGateway", "Latency", "p95", ApiId=api.api_id)],
                    right=[
                        metric("AWS/ApiGateway", "5xx", ApiId=api.api_id),
                        metric("AWS/ApiGateway", "4xx", ApiId=api.api_id),
                    ],
                    width=width,
                ),
                cw.GraphWidget(
                    title="Agente: tokens e custo estimado",
                    left=[tokens_in, tokens_out],
                    right=[cost],
                    width=width,
                ),
            ],
        ]
        cw.Dashboard(self, "Dashboard", dashboard_name=PROJECT, widgets=widgets)
