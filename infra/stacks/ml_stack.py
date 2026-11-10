"""Fase 2: role do SageMaker, Model Registry e VPC opcional para o treino isolado."""

from __future__ import annotations

from typing import Any, Final

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sagemaker as sagemaker
from constructs import Construct

from stacks.common import PROJECT, apply_project_tags
from training.names import MODEL_PACKAGE_GROUP

PHASE: Final = "2"
ISOLATION_AZS: Final = ("us-east-2a", "us-east-2b")
SAGEMAKER_JOB_ACTIONS: Final = (
    "sagemaker:AddTags",
    "sagemaker:CreateModelPackage",
    # O passo de registro do pipeline tenta criar o grupo mesmo quando ele já existe.
    "sagemaker:CreateModelPackageGroup",
    "sagemaker:CreateProcessingJob",
    "sagemaker:CreateTrainingJob",
    "sagemaker:DescribeModelPackage",
    "sagemaker:DescribeModelPackageGroup",
    "sagemaker:DescribeProcessingJob",
    "sagemaker:DescribeTrainingJob",
    "sagemaker:ListTags",
    "sagemaker:StopProcessingJob",
    "sagemaker:StopTrainingJob",
)


class MlStack(Stack):
    """Recursos permanentes do treino. Jobs e pipeline são criados por script."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        curated_bucket: s3.IBucket,
        artifacts_bucket: s3.IBucket,
        key: kms.IKey,
        isolation: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.role = self._create_role(curated_bucket, artifacts_bucket, key)
        sagemaker.CfnModelPackageGroup(
            self,
            "ModelPackageGroup",
            model_package_group_name=MODEL_PACKAGE_GROUP,
            model_package_group_description="Probabilidade de chuva em 1, 3 e 6 h",
        )
        if isolation:
            self._create_isolated_network()

        apply_project_tags(self, PHASE)
        CfnOutput(self, "SageMakerRoleArn", value=self.role.role_arn)
        CfnOutput(self, "ModelPackageGroupName", value=MODEL_PACKAGE_GROUP)

    def _create_role(
        self, curated_bucket: s3.IBucket, artifacts_bucket: s3.IBucket, key: kms.IKey
    ) -> iam.Role:
        role = iam.Role(
            self,
            "SageMakerRole",
            description="Execução do pipeline e dos jobs de treino do chuva-bh",
            assumed_by=iam.ServicePrincipal(
                "sagemaker.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
        )
        for prefix in ("observations/*", "forecasts/*"):
            curated_bucket.grant_read(role, prefix)
        artifacts_bucket.grant_read(role, "sagemaker/*")
        artifacts_bucket.grant_put(role, "sagemaker/*")
        key.grant_encrypt_decrypt(role)

        arn = f"arn:{self.partition}:sagemaker:{self.region}:{self.account}"
        role.add_to_policy(
            iam.PolicyStatement(
                sid="RunPipelineJobs",
                actions=list(SAGEMAKER_JOB_ACTIONS),
                resources=[
                    f"{arn}:processing-job/*",
                    f"{arn}:training-job/*",
                    f"{arn}:model-package/{MODEL_PACKAGE_GROUP}/*",
                    f"{arn}:model-package-group/{MODEL_PACKAGE_GROUP}",
                    f"{arn}:pipeline/{PROJECT}-*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="PassOwnRoleToJobs",
                actions=["iam:PassRole"],
                resources=[role.role_arn],
                conditions={"StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}},
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="JobLogs",
                actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[
                    f"arn:{self.partition}:logs:{self.region}:{self.account}"
                    ":log-group:/aws/sagemaker/*"
                ],
            )
        )
        # As imagens dos algoritmos ficam em contas da AWS que variam por região,
        # e o GetAuthorizationToken não aceita escopo de recurso.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="PullAlgorithmImages",
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                ],
                resources=[f"arn:{self.partition}:ecr:{self.region}:*:repository/*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="EcrToken", actions=["ecr:GetAuthorizationToken"], resources=["*"]
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="JobMetrics",
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringLike": {"cloudwatch:namespace": "/aws/sagemaker/*"}},
            )
        )
        return role

    def _create_isolated_network(self) -> None:
        """VPC sem NAT e sem internet, com acesso ao S3 só pelo gateway endpoint.

        Existe apenas durante o exercício de network isolation: implante com
        `-c isolation=true` e implante de novo sem o contexto para remover.
        """
        vpc = ec2.Vpc(
            self,
            "IsolatedVpc",
            availability_zones=list(ISOLATION_AZS),
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="isolated", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24
                )
            ],
            gateway_endpoints={
                "S3": ec2.GatewayVpcEndpointOptions(service=ec2.GatewayVpcEndpointAwsService.S3)
            },
        )
        # A descrição de security group só aceita ASCII. As subnets não têm rota para a
        # internet, então a saída HTTPS só alcança o S3 pelo gateway endpoint.
        group = ec2.SecurityGroup(
            self,
            "TrainingSecurityGroup",
            vpc=vpc,
            description="Isolated training: no ingress, HTTPS egress reaches S3 only",
            allow_all_outbound=False,
        )
        group.add_egress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS to the S3 gateway")
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="TrainingInVpc",
                actions=[
                    "ec2:CreateNetworkInterface",
                    "ec2:CreateNetworkInterfacePermission",
                    "ec2:DeleteNetworkInterface",
                    "ec2:DeleteNetworkInterfacePermission",
                    "ec2:DescribeDhcpOptions",
                    "ec2:DescribeNetworkInterfaces",
                    "ec2:DescribeSecurityGroups",
                    "ec2:DescribeSubnets",
                    "ec2:DescribeVpcs",
                ],
                resources=["*"],
            )
        )
        CfnOutput(
            self,
            "IsolatedSubnetIds",
            value=",".join(subnet.subnet_id for subnet in vpc.isolated_subnets),
        )
        CfnOutput(self, "IsolatedSecurityGroupId", value=group.security_group_id)
