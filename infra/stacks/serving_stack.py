"""Fase 3: scoring horário, previsões no DynamoDB, HTTP API e deploy na aprovação."""

from __future__ import annotations

import json
from typing import Any, Final

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from serving.store import NO_MODEL
from stacks.bundling import X86_64, bundling_options
from stacks.common import PROJECT, apply_project_tags
from stacks.ingestion_stack import PANDAS_LAYER_ARN, SOURCE_DIR
from stacks.scheduling import RETRY_ATTEMPTS, dead_letter_queue, log_group, schedule
from training.names import MODEL_PACKAGE_GROUP

PHASE: Final = "3"
ENDPOINT_NAME: Final = "chuva-bh-rain"
MODEL_PARAMETER: Final = f"/{PROJECT}/serving/model"
TABLE_NAME: Final = f"{PROJECT}-forecasts"
# Minuto 20: depois da ingestão do minuto 5 e com folga para retentativas.
SCORE_SCHEDULE: Final = "cron(20 * * * ? *)"
API_RATE_LIMIT: Final = 5
API_BURST_LIMIT: Final = 10
PYDANTIC: Final = "pydantic==2.14.3"
_EXCLUDE: Final = [
    "**/__pycache__",
    "**/__pycache__/**",
    "**/*.pyc",
    "agent",
    "agent_main.py",
    "training",
]


class ServingStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        curated_bucket: s3.IBucket,
        sagemaker_role: iam.IRole,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.table = dynamodb.Table(
            self,
            "ForecastsTable",
            table_name=TABLE_NAME,
            partition_key=dynamodb.Attribute(name="point_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="issue_horizon", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            removal_policy=RemovalPolicy.DESTROY,
        )
        # O deployer sobrescreve o valor; o template só cria o parâmetro.
        self.model_parameter = ssm.StringParameter(
            self,
            "ModelParameter",
            parameter_name=MODEL_PARAMETER,
            string_value=json.dumps(NO_MODEL),
            description="Endpoint e versão do modelo em uso pelo scoring",
        )
        code = lambda_.Code.from_asset(str(SOURCE_DIR), exclude=_EXCLUDE)

        self._create_scoring(code, curated_bucket)
        self.api = self._create_api()
        self._create_deployer(code, sagemaker_role)

        apply_project_tags(self, PHASE)
        CfnOutput(self, "ApiUrl", value=self.api.api_endpoint)
        CfnOutput(self, "TableName", value=self.table.table_name)
        CfnOutput(self, "ModelParameterName", value=MODEL_PARAMETER)

    def _arn(self, service: str, resource: str) -> str:
        return f"arn:{self.partition}:{service}:{self.region}:{self.account}:{resource}"

    def _create_scoring(self, code: lambda_.Code, curated_bucket: s3.IBucket) -> None:
        name = f"{PROJECT}-score"
        queue = dead_letter_queue(self, "ScoreDeadLetterQueue", f"{PROJECT}-score-dlq")
        function = lambda_.Function(
            self,
            "ScoreFunction",
            function_name=name,
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="serving.score.handler",
            code=code,
            layers=[
                lambda_.LayerVersion.from_layer_version_arn(self, "PandasLayer", PANDAS_LAYER_ARN)
            ],
            memory_size=1024,
            timeout=Duration.seconds(120),
            log_group=log_group(self, "ScoreLogGroup", name),
            retry_attempts=RETRY_ATTEMPTS,
            dead_letter_queue=queue,
            environment={
                "CURATED_BUCKET": curated_bucket.bucket_name,
                "TABLE_NAME": self.table.table_name,
                "MODEL_PARAMETER": MODEL_PARAMETER,
            },
        )
        for prefix in ("observations/*", "forecasts/*"):
            curated_bucket.grant_read(function, prefix)
        self.table.grant(function, "dynamodb:PutItem")
        self.model_parameter.grant_read(function)
        function.add_to_role_policy(
            iam.PolicyStatement(
                sid="InvokeRainEndpoint",
                actions=["sagemaker:InvokeEndpoint"],
                resources=[self._arn("sagemaker", f"endpoint/{ENDPOINT_NAME}")],
            )
        )
        function.add_to_role_policy(
            iam.PolicyStatement(
                sid="PublishServingMetrics",
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": "ChuvaBh/Serving"}},
            )
        )
        schedule(
            self,
            "ScoreSchedule",
            name=f"{PROJECT}-score-hourly",
            expression=SCORE_SCHEDULE,
            function=function,
            queue=queue,
        )

    def _create_api(self) -> apigw.HttpApi:
        name = f"{PROJECT}-api"
        code = lambda_.Code.from_asset(
            str(SOURCE_DIR),
            bundling=bundling_options(
                SOURCE_DIR,
                packages=("features", "ingestion", "serving"),
                requirements=(PYDANTIC,),
                platform=X86_64,
                image=lambda_.Runtime.PYTHON_3_12.bundling_image,
            ),
        )
        function = lambda_.Function(
            self,
            "ApiFunction",
            function_name=name,
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="serving.api.handler",
            code=code,
            memory_size=256,
            timeout=Duration.seconds(10),
            log_group=log_group(self, "ApiLogGroup", name),
            environment={"TABLE_NAME": self.table.table_name},
        )
        self.table.grant(function, "dynamodb:Query")

        # Autenticação IAM: só identidades da conta (o agente e a CLI) chamam a API.
        api = apigw.HttpApi(self, "HttpApi", api_name=name, create_default_stage=False)
        apigw.HttpStage(
            self,
            "DefaultStage",
            http_api=api,
            stage_name="$default",
            auto_deploy=True,
            throttle=apigw.ThrottleSettings(rate_limit=API_RATE_LIMIT, burst_limit=API_BURST_LIMIT),
        )
        integration = integrations.HttpLambdaIntegration("ApiIntegration", function)
        for path in ("/points", "/forecast"):
            api.add_routes(
                path=path,
                methods=[apigw.HttpMethod.GET],
                integration=integration,
                authorizer=authorizers.HttpIamAuthorizer(),
            )
        return api

    def _create_deployer(self, code: lambda_.Code, sagemaker_role: iam.IRole) -> None:
        name = f"{PROJECT}-deployer"
        queue = dead_letter_queue(self, "DeployerDeadLetterQueue", f"{PROJECT}-deployer-dlq")
        function = lambda_.Function(
            self,
            "DeployerFunction",
            function_name=name,
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="serving.deployer.handler",
            code=code,
            memory_size=256,
            timeout=Duration.seconds(60),
            log_group=log_group(self, "DeployerLogGroup", name),
            environment={
                "SAGEMAKER_ROLE_ARN": sagemaker_role.role_arn,
                "ENDPOINT_NAME": ENDPOINT_NAME,
                "MODEL_PARAMETER": MODEL_PARAMETER,
                "MODEL_PACKAGE_GROUP": MODEL_PACKAGE_GROUP,
            },
        )
        self.model_parameter.grant_write(function)
        function.add_to_role_policy(
            iam.PolicyStatement(
                sid="DeployApprovedModel",
                actions=[
                    "sagemaker:AddTags",
                    "sagemaker:CreateEndpoint",
                    "sagemaker:CreateEndpointConfig",
                    "sagemaker:CreateModel",
                    "sagemaker:DescribeEndpoint",
                    "sagemaker:DescribeModelPackage",
                    "sagemaker:UpdateEndpoint",
                ],
                resources=[
                    self._arn("sagemaker", f"model-package/{MODEL_PACKAGE_GROUP}/*"),
                    self._arn("sagemaker", f"model/{ENDPOINT_NAME}-v*"),
                    self._arn("sagemaker", f"endpoint-config/{ENDPOINT_NAME}-v*"),
                    self._arn("sagemaker", f"endpoint/{ENDPOINT_NAME}"),
                ],
            )
        )
        function.add_to_role_policy(
            iam.PolicyStatement(
                sid="PassModelRole",
                actions=["iam:PassRole"],
                resources=[sagemaker_role.role_arn],
                conditions={"StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}},
            )
        )
        events.Rule(
            self,
            "ModelApprovedRule",
            description="Modelo aprovado no Model Registry dispara o deploy",
            event_pattern=events.EventPattern(
                source=["aws.sagemaker"],
                detail_type=["SageMaker Model Package State Change"],
                detail={
                    "ModelPackageGroupName": [MODEL_PACKAGE_GROUP],
                    "ModelApprovalStatus": ["Approved"],
                },
            ),
            targets=[
                targets.LambdaFunction(
                    function, retry_attempts=RETRY_ATTEMPTS, dead_letter_queue=queue
                )
            ],
        )
