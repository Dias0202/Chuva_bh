"""Fase 6: site com previsões públicas e chat com o agente protegido por login."""

from __future__ import annotations

from typing import Any, Final

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from constructs import Construct

from stacks.bundling import X86_64, bundling_options
from stacks.common import PROJECT, apply_project_tags
from stacks.ingestion_stack import SOURCE_DIR
from stacks.scheduling import dead_letter_queue, log_group, schedule
from stacks.serving_stack import PYDANTIC

PHASE: Final = "6"
# Minuto 30: depois do scoring do minuto 20.
PUBLISH_SCHEDULE: Final = "cron(30 * * * ? *)"
TOKEN_HEADER: Final = "x-chuva-token"  # noqa: S105 - nome do cabeçalho, não um segredo
CHAT_RATE_LIMIT: Final = 1
CHAT_BURST_LIMIT: Final = 3
# Só o próprio site, o Cognito para o login e nada de terceiros.
CONTENT_SECURITY_POLICY: Final = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self' https://cognito-idp.{region}.amazonaws.com; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


class WebStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        key: kms.IKey,
        table: dynamodb.ITable,
        agent_runtime_arn: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.bucket = s3.Bucket(
            self,
            "SiteBucket",
            bucket_name=f"{PROJECT}-web-{self.account}-{self.region}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        user_pool, client = self._create_user_pool()
        api = self._create_chat_api(user_pool, client, agent_runtime_arn)
        distribution = self._create_distribution(api)
        self._create_publisher(table)

        apply_project_tags(self, PHASE)
        CfnOutput(self, "SiteUrl", value=f"https://{distribution.distribution_domain_name}")
        CfnOutput(self, "DistributionId", value=distribution.distribution_id)
        CfnOutput(self, "SiteBucketName", value=self.bucket.bucket_name)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=client.user_pool_client_id)

    def _create_user_pool(self) -> tuple[cognito.UserPool, cognito.UserPoolClient]:
        """Sem auto-cadastro: os usuários são criados por script e recebem senha temporária."""
        user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"{PROJECT}-site",
            feature_plan=cognito.FeaturePlan.LITE,
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_digits=True,
                require_lowercase=True,
                require_uppercase=True,
                require_symbols=False,
                temp_password_validity=Duration.days(3),
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.DESTROY,
        )
        client = user_pool.add_client(
            "SiteClient",
            user_pool_client_name=f"{PROJECT}-site",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_password=True),
            prevent_user_existence_errors=True,
            id_token_validity=Duration.hours(1),
            access_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(1),
        )
        return user_pool, client

    def _code(self) -> lambda_.Code:
        return lambda_.Code.from_asset(
            str(SOURCE_DIR),
            bundling=bundling_options(
                SOURCE_DIR,
                packages=("features", "ingestion", "serving", "web"),
                requirements=(PYDANTIC,),
                platform=X86_64,
                image=lambda_.Runtime.PYTHON_3_12.bundling_image,
            ),
        )

    def _create_chat_api(
        self,
        user_pool: cognito.IUserPool,
        client: cognito.IUserPoolClient,
        agent_runtime_arn: str,
    ) -> apigw.HttpApi:
        name = f"{PROJECT}-chat"
        function = lambda_.Function(
            self,
            "ChatFunction",
            function_name=name,
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="web.chat.handler",
            code=self._code(),
            memory_size=256,
            # Abaixo dos 30 s de limite da integração da HTTP API.
            timeout=Duration.seconds(28),
            log_group=log_group(self, "ChatLogGroup", name),
            environment={"AGENT_RUNTIME_ARN": agent_runtime_arn},
        )
        function.add_to_role_policy(
            iam.PolicyStatement(
                sid="InvokeAgent",
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[agent_runtime_arn, f"{agent_runtime_arn}/*"],
            )
        )
        api = apigw.HttpApi(self, "ChatApi", api_name=name, create_default_stage=False)
        apigw.HttpStage(
            self,
            "ChatStage",
            http_api=api,
            stage_name="$default",
            auto_deploy=True,
            throttle=apigw.ThrottleSettings(
                rate_limit=CHAT_RATE_LIMIT, burst_limit=CHAT_BURST_LIMIT
            ),
        )
        api.add_routes(
            path="/api/chat",
            methods=[apigw.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration("ChatIntegration", function),
            # Token em cabeçalho próprio: com cache desligado, o CloudFront não garante o repasse
            # do Authorization para a origem.
            authorizer=authorizers.HttpUserPoolAuthorizer(
                "ChatAuthorizer",
                user_pool,
                user_pool_clients=[client],
                identity_source=[f"$request.header.{TOKEN_HEADER}"],
            ),
        )
        return api

    def _create_distribution(self, api: apigw.HttpApi) -> cloudfront.Distribution:
        headers = cloudfront.ResponseHeadersPolicy(
            self,
            "SecurityHeaders",
            response_headers_policy_name=f"{PROJECT}-site",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=CONTENT_SECURITY_POLICY.format(region=self.region),
                    override=True,
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365),
                    include_subdomains=True,
                    override=True,
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.NO_REFERRER, override=True
                ),
            ),
        )
        api_origin = origins.HttpOrigin(f"{api.api_id}.execute-api.{self.region}.amazonaws.com")
        return cloudfront.Distribution(
            self,
            "Distribution",
            comment="Site do chuva-bh",
            default_root_object="index.html",
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(self.bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=headers,
            ),
            additional_behaviors={
                "/api/*": cloudfront.BehaviorOptions(
                    origin=api_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    # Repassa os cabeçalhos do navegador, com o token, sem o Host do site.
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                    response_headers_policy=headers,
                )
            },
        )

    def _create_publisher(self, table: dynamodb.ITable) -> None:
        name = f"{PROJECT}-publish"
        queue = dead_letter_queue(self, "PublishDeadLetterQueue", f"{name}-dlq")
        function = lambda_.Function(
            self,
            "PublishFunction",
            function_name=name,
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="web.publisher.handler",
            code=self._code(),
            memory_size=256,
            timeout=Duration.seconds(30),
            log_group=log_group(self, "PublishLogGroup", name),
            dead_letter_queue=queue,
            environment={"TABLE_NAME": table.table_name, "SITE_BUCKET": self.bucket.bucket_name},
        )
        table.grant(function, "dynamodb:Query")
        self.bucket.grant_put(function, "data/*")
        schedule(
            self,
            "PublishSchedule",
            name=f"{name}-hourly",
            expression=PUBLISH_SCHEDULE,
            function=function,
            queue=queue,
        )
