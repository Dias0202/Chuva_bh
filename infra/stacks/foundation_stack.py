"""Fase 0: criptografia, buckets, auditoria e controle de custo."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_cloudtrail as cloudtrail
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from constructs import Construct

from stacks.common import PROJECT, apply_project_tags, project_tags

PHASE: Final = "0"

BUDGET_NAME: Final = f"{PROJECT}-total"
BUDGET_LIMIT_USD: Final = 60
# O orçamento é do projeto inteiro. Um período mensal reiniciaria o acumulado na
# virada do mês e permitiria gastar mais do que os créditos disponíveis.
BUDGET_TIME_UNIT: Final = "ANNUALLY"
BUDGET_START: Final = datetime(2026, 11, 1, tzinfo=UTC)
ALERT_THRESHOLDS_PERCENT: Final = (25, 50, 80, 100)
KILL_SWITCH_THRESHOLD_PERCENT: Final = 90

KILL_SWITCH_POLICY_NAME: Final = f"{PROJECT}-kill-switch-deny"
# Somente ações que criam ou iniciam trabalho: o teardown precisa continuar
# conseguindo apagar recursos depois que o kill switch disparar.
KILL_SWITCH_DENIED_ACTIONS: Final = (
    "bedrock-agentcore:Create*",
    "bedrock:Create*",
    "sagemaker:Create*",
    "sagemaker:StartPipelineExecution",
)

CDK_QUALIFIER: Final = "hnb659fds"

DATA_BUCKETS: Final = ("raw", "curated", "artifacts", "kb-docs")
AUDIT_BUCKET: Final = "audit"
TRAIL_NAME: Final = f"{PROJECT}-trail"
RAW_EXPIRATION_DAYS: Final = 30
AUDIT_EXPIRATION_DAYS: Final = 90
INCOMPLETE_UPLOAD_DAYS: Final = 7

_EMAIL_PATTERN: Final = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_IAM_USER_PATTERN: Final = re.compile(r"^[\w+=,.@-]{1,64}$")
# Nome de role com caminho opcional, como "managed/AccountFullAccessRole".
_IAM_ROLE_PATTERN: Final = re.compile(r"^([\w+=,.@-]+/)*[\w+=,.@-]{1,64}$")


@dataclass(frozen=True)
class FoundationConfig:
    """Parâmetros de implantação que não ficam versionados no repositório."""

    budget_email: str
    kill_switch_users: tuple[str, ...] = ()
    kill_switch_roles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _EMAIL_PATTERN.fullmatch(self.budget_email):
            raise ValueError("budget_email deve ser um endereço de e-mail válido")
        invalid = [u for u in self.kill_switch_users if not _IAM_USER_PATTERN.fullmatch(u)]
        if invalid:
            raise ValueError(f"nomes de usuário IAM inválidos: {invalid}")
        invalid = [r for r in self.kill_switch_roles if not _IAM_ROLE_PATTERN.fullmatch(r)]
        if invalid:
            raise ValueError(f"nomes de role IAM inválidos: {invalid}")


class FoundationStack(Stack):
    """Recursos-base dos quais as demais fases dependem."""

    def __init__(
        self, scope: Construct, construct_id: str, *, config: FoundationConfig, **kwargs: Any
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.key = self._create_key()
        self.buckets = {name: self._create_bucket(name) for name in DATA_BUCKETS}
        self._create_audit_trail()
        self._create_budget(config)

        apply_project_tags(self, PHASE)
        CfnOutput(self, "KmsKeyArn", value=self.key.key_arn)
        for name, bucket in self.buckets.items():
            CfnOutput(self, f"{name.title().replace('-', '')}BucketName", value=bucket.bucket_name)

    def _create_key(self) -> kms.Key:
        return kms.Key(
            self,
            "ProjectKey",
            alias=f"alias/{PROJECT}",
            description=f"Chave de criptografia do projeto {PROJECT}",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
            pending_window=Duration.days(7),
        )

    def _create_bucket(
        self, name: str, lifecycle_rules: list[s3.LifecycleRule] | None = None
    ) -> s3.Bucket:
        expiration_days = {"raw": RAW_EXPIRATION_DAYS, AUDIT_BUCKET: AUDIT_EXPIRATION_DAYS}.get(
            name
        )
        rules = list(lifecycle_rules or [])
        rules.append(
            s3.LifecycleRule(
                id="abort-incomplete-uploads",
                abort_incomplete_multipart_upload_after=Duration.days(INCOMPLETE_UPLOAD_DAYS),
            )
        )
        if expiration_days is not None:
            rules.append(s3.LifecycleRule(id="expire", expiration=Duration.days(expiration_days)))

        # Sem auto_delete_objects: o teardown esvazia os buckets, o que evita uma
        # Lambda de custom resource e a role associada.
        return s3.Bucket(
            self,
            f"{name.title().replace('-', '')}Bucket",
            bucket_name=f"{PROJECT}-{name}-{self.account}-{self.region}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            enforce_ssl=True,
            lifecycle_rules=rules,
            removal_policy=RemovalPolicy.DESTROY,
        )

    def _create_audit_trail(self) -> None:
        audit_bucket = self._create_bucket(AUDIT_BUCKET)
        trail_arn = (
            f"arn:{self.partition}:cloudtrail:{self.region}:{self.account}:trail/{TRAIL_NAME}"
        )

        # O ARN é montado à mão porque a trail depende da chave: referenciar o
        # atributo da trail na política da chave criaria uma dependência circular.
        self.key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudTrailEncrypt",
                principals=[iam.ServicePrincipal("cloudtrail.amazonaws.com")],
                actions=["kms:GenerateDataKey*"],
                resources=["*"],
                conditions={
                    "StringEquals": {"aws:SourceArn": trail_arn},
                    "StringLike": {
                        "kms:EncryptionContext:aws:cloudtrail:arn": (
                            f"arn:{self.partition}:cloudtrail:*:{self.account}:trail/*"
                        )
                    },
                },
            )
        )
        self.key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudTrailDescribeKey",
                principals=[iam.ServicePrincipal("cloudtrail.amazonaws.com")],
                actions=["kms:DescribeKey"],
                resources=["*"],
                conditions={"StringEquals": {"aws:SourceArn": trail_arn}},
            )
        )

        cloudtrail.Trail(
            self,
            "AuditTrail",
            trail_name=TRAIL_NAME,
            bucket=audit_bucket,
            encryption_key=self.key,
            is_multi_region_trail=True,
            include_global_service_events=True,
            enable_file_validation=True,
            send_to_cloud_watch_logs=False,
        )

    def _cdk_role_name(self, kind: str) -> str:
        return f"cdk-{CDK_QUALIFIER}-{kind}-{self.account}-{self.region}"

    def _create_budget(self, config: FoundationConfig) -> None:
        subscriber = budgets.CfnBudget.SubscriberProperty(
            address=config.budget_email, subscription_type="EMAIL"
        )
        budget = budgets.CfnBudget(
            self,
            "ProjectBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name=BUDGET_NAME,
                budget_type="COST",
                time_unit=BUDGET_TIME_UNIT,
                time_period=budgets.CfnBudget.TimePeriodProperty(
                    start=str(int(BUDGET_START.timestamp()))
                ),
                budget_limit=budgets.CfnBudget.SpendProperty(amount=BUDGET_LIMIT_USD, unit="USD"),
                # A conta é paga com créditos. Com o padrão (IncludeCredit=true) o
                # custo líquido fica em zero e nenhum alerta dispararia.
                cost_types=budgets.CfnBudget.CostTypesProperty(
                    include_credit=False, include_refund=False
                ),
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        comparison_operator="GREATER_THAN",
                        notification_type="ACTUAL",
                        threshold=threshold,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[subscriber],
                )
                for threshold in ALERT_THRESHOLDS_PERCENT
            ],
            resource_tags=[
                budgets.CfnBudget.ResourceTagProperty(key=key, value=value)
                for key, value in project_tags(PHASE).items()
            ],
        )

        deny_policy = iam.ManagedPolicy(
            self,
            "KillSwitchDenyPolicy",
            managed_policy_name=KILL_SWITCH_POLICY_NAME,
            description="Anexada pelo Budget Action: bloqueia criação em SageMaker e Bedrock",
            statements=[
                iam.PolicyStatement(
                    sid="DenyCostlyResourceCreation",
                    effect=iam.Effect.DENY,
                    actions=list(KILL_SWITCH_DENIED_ACTIONS),
                    # Os recursos bloqueados ainda não existem, logo não há ARN a citar.
                    resources=["*"],
                )
            ],
        )

        # O CloudFormation cria os recursos com a cfn-exec-role do bootstrap, portanto
        # é nela que o deny precisa estar para valer sobre o que o CDK implanta. Roles extras
        # cobrem o que é criado fora do CDK, como os jobs iniciados por script.
        role_paths = [self._cdk_role_name("cfn-exec-role"), *config.kill_switch_roles]
        # O Budget Action recebe só o nome; o ARN da permissão precisa do caminho da role.
        target_roles = [path.rsplit("/", 1)[-1] for path in role_paths]
        execution_role = self._create_kill_switch_execution_role(
            deny_policy, role_paths, config.kill_switch_users
        )

        action = budgets.CfnBudgetsAction(
            self,
            "KillSwitchAction",
            budget_name=budget.ref,
            action_type="APPLY_IAM_POLICY",
            approval_model="AUTOMATIC",
            notification_type="ACTUAL",
            action_threshold=budgets.CfnBudgetsAction.ActionThresholdProperty(
                type="PERCENTAGE", value=KILL_SWITCH_THRESHOLD_PERCENT
            ),
            definition=budgets.CfnBudgetsAction.DefinitionProperty(
                iam_action_definition=budgets.CfnBudgetsAction.IamActionDefinitionProperty(
                    policy_arn=deny_policy.managed_policy_arn,
                    roles=target_roles,
                    users=list(config.kill_switch_users) or None,
                )
            ),
            execution_role_arn=execution_role.role_arn,
            subscribers=[
                budgets.CfnBudgetsAction.SubscriberProperty(
                    address=config.budget_email, type="EMAIL"
                )
            ],
            resource_tags=[
                budgets.CfnBudgetsAction.ResourceTagProperty(key=key, value=value)
                for key, value in project_tags(PHASE).items()
            ],
        )
        action.node.add_dependency(execution_role)

    def _create_kill_switch_execution_role(
        self,
        deny_policy: iam.ManagedPolicy,
        role_paths: list[str],
        target_users: tuple[str, ...],
    ) -> iam.Role:
        role = iam.Role(
            self,
            "KillSwitchExecutionRole",
            description="Permite ao AWS Budgets anexar a política de deny do kill switch",
            assumed_by=iam.ServicePrincipal(
                "budgets.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
        )
        only_deny_policy = {"ArnEquals": {"iam:PolicyARN": deny_policy.managed_policy_arn}}
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ManageDenyPolicyOnRoles",
                actions=["iam:AttachRolePolicy", "iam:DetachRolePolicy"],
                resources=[
                    f"arn:{self.partition}:iam::{self.account}:role/{path}" for path in role_paths
                ],
                conditions=only_deny_policy,
            )
        )
        if target_users:
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="ManageDenyPolicyOnUsers",
                    actions=["iam:AttachUserPolicy", "iam:DetachUserPolicy"],
                    resources=[
                        f"arn:{self.partition}:iam::{self.account}:user/{name}"
                        for name in target_users
                    ],
                    conditions=only_deny_policy,
                )
            )
        return role
